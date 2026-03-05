"""
Price Feed — Binance Futures WebSocket (1m kline stream)
Simülasyon modu da dahil.
"""

import asyncio
import json
import logging
import math
import random
import time

import websockets

from src.bot import Candle, TrendBreakBot

logger = logging.getLogger(__name__)


class BinanceFeed:
    """Live 1-minute kline feed from Binance Futures."""

    WS_BASE = "wss://fstream.binance.com/ws"

    def __init__(self, bot: TrendBreakBot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        symbol = self.bot.config.symbol.lower()
        url = f"{self.WS_BASE}/{symbol}@kline_1m"
        logger.info(f"Connecting to Binance: {url}")

        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Binance WS connected.")
                    async for raw in ws:
                        if not self.bot.running:
                            break
                        data = json.loads(raw)
                        k = data.get("k", {})
                        if k.get("x"):   # candle closed
                            candle = Candle(
                                open=float(k["o"]),
                                high=float(k["h"]),
                                low=float(k["l"]),
                                close=float(k["c"]),
                                volume=float(k["v"]),
                                timestamp=int(k["t"]),
                            )
                            await self.bot.on_new_candle(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Binance WS error: {exc}. Reconnecting in 5s...")
                await asyncio.sleep(5)


class SimFeed:
    """
    Simülasyon fiyat akışı.
    Her X saniyede bir 1-dakikalık mum üretir (hızlandırılmış).
    """

    def __init__(self, bot: TrendBreakBot, tick_interval: float = 2.0):
        self.bot = bot
        self.tick_interval = tick_interval
        self._task: asyncio.Task | None = None

        # Başlangıç fiyatı
        self._price = 67_000.0
        self._trend = 1          # +1 yükseliş, -1 düşüş
        self._volatility = 0.0008
        self._trend_strength = 0.52  # >0.5 → yukarı bias

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self):
        logger.info(f"SimFeed başlatıldı — tick={self.tick_interval}s")
        while self.bot.running:
            candle = self._next_candle()
            await self.bot.on_new_candle(candle)
            await asyncio.sleep(self.tick_interval)

    def _next_candle(self) -> Candle:
        open_p = self._price
        high_p = open_p
        low_p = open_p
        close_p = open_p

        steps = 12
        for _ in range(steps):
            bias = self._trend_strength if self._trend > 0 else (1 - self._trend_strength)
            r = (random.random() - (1 - bias)) * self._volatility * 2
            close_p *= (1 + r)
            high_p = max(high_p, close_p)
            low_p = min(low_p, close_p)

        # Occasional trend flip
        if random.random() < 0.03:
            self._trend *= -1
            self._volatility = 0.0006 + random.random() * 0.0014
            logger.debug(f"Trend flip → {'UP' if self._trend > 0 else 'DOWN'}")

        self._price = close_p
        ts = int(time.time() * 1000)

        return Candle(
            open=round(open_p, 2),
            high=round(high_p, 2),
            low=round(low_p, 2),
            close=round(close_p, 2),
            volume=round(random.uniform(10, 100), 2),
            timestamp=ts,
        )
