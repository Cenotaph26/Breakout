"""Price Feed — Binance WS + Sim."""

import asyncio, json, logging, random, time
import websockets
from src.bot import Candle, Bot

log = logging.getLogger("feed")

TESTNET_WS = "wss://fstream.binancefuture.com/ws"
LIVE_WS = "wss://fstream.binance.com/ws"


class BinanceFeed:
    def __init__(self, bot: Bot, demo=True):
        self.bot = bot
        self.demo = demo
        self._tasks = []

    def _ws(self):
        return TESTNET_WS if self.demo else LIVE_WS

    async def start(self):
        self._tasks = [
            asyncio.create_task(self._kline_loop()),
            asyncio.create_task(self._ticker_loop()),
        ]
        log.info("Feed started")

    async def stop(self):
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except:
                pass
        self._tasks = []
        log.info("Feed stopped")

    async def _kline_loop(self):
        sym = self.bot.cfg.symbol.lower()
        url = f"{self._ws()}/{sym}@kline_1m"
        log.info(f"Kline stream → {url}")
        candle_count = 0
        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=15, close_timeout=5) as ws:
                    log.info("Kline WS bağlandı")
                    async for raw in ws:
                        if not self.bot.running:
                            break
                        try:
                            d = json.loads(raw)
                            k = d.get("k", {})
                            if not k:
                                continue

                            price = float(k.get("c", 0))
                            if price > 0:
                                self.bot.price = price
                                self.bot.live_candle = {
                                    "o": float(k["o"]), "h": float(k["h"]),
                                    "l": float(k["l"]), "c": price, "t": int(k["t"]),
                                }

                            if k.get("x"):  # Mum kapandı
                                candle_count += 1
                                c = Candle(
                                    o=float(k["o"]), h=float(k["h"]),
                                    l=float(k["l"]), c=float(k["c"]),
                                    v=float(k["v"]), t=int(k["t"]),
                                )
                                self.bot.live_candle = None
                                log.info(f"MUM #{candle_count} kapandı: O={c.o:.2f} H={c.h:.2f} L={c.l:.2f} C={c.c:.2f}")
                                await self.bot.on_candle(c)
                        except Exception as e:
                            log.debug(f"Kline parse: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Kline WS hata: {e} — 5s bekle")
                await asyncio.sleep(5)

    async def _ticker_loop(self):
        sym = self.bot.cfg.symbol.lower()
        url = f"{self._ws()}/{sym}@miniTicker"
        log.info(f"Ticker stream → {url}")
        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=15, close_timeout=5) as ws:
                    log.info("Ticker WS bağlandı")
                    async for raw in ws:
                        if not self.bot.running:
                            break
                        try:
                            d = json.loads(raw)
                            if "c" in d:
                                await self.bot.on_tick(float(d["c"]))
                        except:
                            pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Ticker WS hata: {e} — 5s bekle")
                await asyncio.sleep(5)


class SimFeed:
    def __init__(self, bot: Bot, interval=2.0):
        self.bot = bot
        self.interval = interval
        self._task = None
        self._p = 67000.0
        self._trend = 1
        self._tc = 0
        self._td = random.randint(6, 15)
        self._vol = 0.0018
        self._mom = 0.0

    async def start(self):
        self._task = asyncio.create_task(self._run())
        log.info("SimFeed started")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except:
                pass
        log.info("SimFeed stopped")

    async def _run(self):
        while self.bot.running:
            c = self._gen()
            self.bot.price = c.c
            await self.bot.on_candle(c)
            await asyncio.sleep(self.interval)

    def _gen(self) -> Candle:
        op = self._p
        bias = 0.56 if self._trend > 0 else 0.44
        self._mom = self._mom * 0.7 + self._trend * 0.0003
        cp = hp = lp = op
        for _ in range(10):
            r = (random.random() - (1 - bias)) * self._vol * 2 + self._mom
            cp *= (1 + r)
            hp = max(hp, cp)
            lp = min(lp, cp)
        wick = abs(cp - op) * random.uniform(0.1, 0.4)
        if self._trend > 0:
            hp = max(hp, cp + wick)
        else:
            lp = min(lp, cp - wick)
        self._tc += 1
        if self._tc >= self._td:
            self._trend *= -1
            self._tc = 0
            self._td = random.randint(5, 20)
            self._mom = 0
            self._vol = random.uniform(0.002, 0.0035)
        else:
            self._vol = max(0.0012, self._vol * 0.95)
        self._p = cp
        return Candle(
            o=round(op, 2), h=round(max(op, hp), 2),
            l=round(min(op, lp), 2), c=round(cp, 2),
            v=round(random.uniform(20, 200), 2), t=int(time.time() * 1000),
        )
