"""
Price Feed
BinanceFeed: Testnet/Live USDT-M Futures
- kline_1m stream  → kapanan mumda strateji tetikler
- miniTicker stream → her ~1s anlık fiyat günceller (grafik canlı kalır)
"""

import asyncio
import json
import logging
import random
import time

import websockets

from src.bot import Candle, TrendBreakBot

logger = logging.getLogger(__name__)

# Testnet WS:  wss://fstream.binancefuture.com/ws  (REST ile aynı host değil!)
# Live WS:     wss://fstream.binance.com/ws
TESTNET_WS = "wss://fstream.binancefuture.com/ws"
LIVE_WS    = "wss://fstream.binance.com/ws"


class BinanceFeed:
    def __init__(self, bot: TrendBreakBot, demo: bool = True):
        self.bot  = bot
        self.demo = demo
        self._kline_task:  asyncio.Task | None = None
        self._ticker_task: asyncio.Task | None = None

    def _ws_base(self):
        return TESTNET_WS if self.demo else LIVE_WS

    async def start(self):
        self._kline_task  = asyncio.create_task(self._kline_stream())
        self._ticker_task = asyncio.create_task(self._ticker_stream())

    async def stop(self):
        for t in (self._kline_task, self._ticker_task):
            if t:
                t.cancel()
                try:    await t
                except: pass
        self._kline_task = self._ticker_task = None

    async def _kline_stream(self):
        sym = self.bot.config.symbol.lower()
        url = f"{self._ws_base()}/{sym}@kline_1m"
        logger.info(f"Kline stream → {url}")
        while self.bot.running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=15,   # her 15s Binance'e ping
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("✅ Kline stream bağlandı")
                    async for raw in ws:
                        if not self.bot.running: break
                        try:
                            d = json.loads(raw)
                            k = d.get("k", {})
                            self.bot.current_price = float(k.get("c", self.bot.current_price))
                            if k.get("x"):
                                candle = Candle(
                                    open=float(k["o"]), high=float(k["h"]),
                                    low=float(k["l"]),  close=float(k["c"]),
                                    volume=float(k["v"]), timestamp=int(k["t"]),
                                )
                                await self.bot.on_new_candle(candle)
                        except Exception as ex:
                            logger.debug(f"Kline parse: {ex}")
            except asyncio.CancelledError: raise
            except Exception as e:
                logger.warning(f"Kline stream hata: {e} — 3s")
                await asyncio.sleep(3)

    async def _ticker_stream(self):
        sym = self.bot.config.symbol.lower()
        url = f"{self._ws_base()}/{sym}@miniTicker"
        logger.info(f"Ticker stream → {url}")
        while self.bot.running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=15,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("✅ Ticker stream bağlandı")
                    async for raw in ws:
                        if not self.bot.running: break
                        try:
                            d = json.loads(raw)
                            if "c" in d:
                                self.bot.current_price = float(d["c"])
                        except: pass
            except asyncio.CancelledError: raise
            except Exception as e:
                logger.warning(f"Ticker stream hata: {e} — 3s")
                await asyncio.sleep(3)


class SimFeed:
    def __init__(self, bot: TrendBreakBot, tick_interval: float = 2.0):
        self.bot           = bot
        self.tick_interval = tick_interval
        self._task: asyncio.Task | None = None
        self._price          = 67_000.0
        self._trend          = 1
        self._trend_candles  = 0
        self._trend_duration = random.randint(6, 15)
        self._volatility     = 0.0018
        self._momentum       = 0.0

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:    await self._task
            except: pass
            self._task = None

    async def _run(self):
        logger.info(f"SimFeed başlatıldı — tick={self.tick_interval}s")
        while self.bot.running:
            c = self._next_candle()
            self.bot.current_price = c.close
            await self.bot.on_new_candle(c)
            await asyncio.sleep(self.tick_interval)

    def _next_candle(self) -> Candle:
        op = self._price
        bias = 0.56 if self._trend > 0 else 0.44
        self._momentum = self._momentum * 0.7 + self._trend * 0.0003
        cp = hp = lp = op
        for _ in range(10):
            r  = (random.random() - (1 - bias)) * self._volatility * 2 + self._momentum
            cp *= (1 + r)
            hp  = max(hp, cp)
            lp  = min(lp, cp)
        wick = abs(cp - op) * random.uniform(0.1, 0.4)
        if self._trend > 0: hp = max(hp, cp + wick)
        else:               lp = min(lp, cp - wick)
        self._trend_candles += 1
        if self._trend_candles >= self._trend_duration:
            self._trend *= -1; self._trend_candles = 0
            self._trend_duration = random.randint(5, 20)
            self._momentum = 0.0
            self._volatility = random.uniform(0.0020, 0.0035)
        else:
            self._volatility = max(0.0012, self._volatility * 0.95)
        self._price = cp
        return Candle(
            open=round(op,2), high=round(max(op,hp),2),
            low=round(min(op,lp),2), close=round(cp,2),
            volume=round(random.uniform(20,200),2),
            timestamp=int(time.time()*1000),
        )
