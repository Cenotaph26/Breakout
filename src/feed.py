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
    """
    Live 1-minute kline feed from Binance.
    account_type: SPOT | USDT_FUTURES | COIN_FUTURES
    Demo hesap için normal API key + BinanceEnvironment.DEMO kullanılır.
    """

    # Spot public stream
    WS_SPOT    = "wss://stream.binance.com:9443/ws"
    # Futures public stream
    WS_FUTURES = "wss://fstream.binance.com/ws"

    def __init__(self, bot: TrendBreakBot,
                 api_key: str = "",
                 api_secret: str = "",
                 account_type: str = "USDT_FUTURES"):
        self.bot          = bot
        self.api_key      = api_key
        self.api_secret   = api_secret
        self.account_type = account_type.upper()
        self._task: asyncio.Task | None = None

    def _ws_url(self) -> str:
        symbol = self.bot.config.symbol.lower()
        # Perpetual futures symbol: BTCUSDT-PERP → btcusdt, BTCUSDT → btcusdt
        sym = symbol.replace("-perp", "").replace("-", "")
        if self.account_type in ("USDT_FUTURES", "COIN_FUTURES"):
            return f"{self.WS_FUTURES}/{sym}@kline_1m"
        return f"{self.WS_SPOT}/{sym}@kline_1m"

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
        url = self._ws_url()
        logger.info(f"Binance bağlanıyor: {url} | account={self.account_type}")

        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    logger.info("Binance WS bağlandı.")
                    async for raw in ws:
                        if not self.bot.running:
                            break
                        data = json.loads(raw)
                        k = data.get("k", {})
                        if k.get("x"):   # kapalı mum
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
                logger.warning(f"Binance WS hatası: {exc}. 5sn sonra yeniden bağlanıyor...")
                await asyncio.sleep(5)


class SimFeed:
    """
    Simülasyon fiyat akışı — gerçekçi trend + kırılma davranışı.
    Her tick_interval saniyede bir kapalı 1m mum üretir.
    """

    def __init__(self, bot: TrendBreakBot, tick_interval: float = 2.0):
        self.bot = bot
        self.tick_interval = tick_interval
        self._task: asyncio.Task | None = None

        self._price = 67_000.0
        self._trend = 1            # +1 yükseliş, -1 düşüş
        self._trend_candles = 0    # kaç mumdur aynı trend sürüyor
        self._trend_duration = random.randint(6, 15)  # trend kaç mum sürecek
        self._volatility = 0.0018  # daha yüksek volatilite → daha fazla kırılma
        self._momentum = 0.0       # birikmiş yön kuvveti

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
        logger.info(f"SimFeed başlatıldı — tick={self.tick_interval}s | vol={self._volatility}")
        while self.bot.running:
            candle = self._next_candle()
            await self.bot.on_new_candle(candle)
            await asyncio.sleep(self.tick_interval)

    def _next_candle(self) -> Candle:
        open_p = self._price

        # Trend yönüne göre güçlü momentum bias
        # trend=+1 → yukarı, trend=-1 → aşağı
        bias = 0.56 if self._trend > 0 else 0.44  # Belirgin yön kuvveti

        # Momentum birikimi (trend içinde hızlanma)
        self._momentum = self._momentum * 0.7 + (self._trend * 0.0003)

        steps = 10
        close_p = open_p
        high_p = open_p
        low_p = open_p

        for _ in range(steps):
            r = (random.random() - (1 - bias)) * self._volatility * 2
            r += self._momentum  # momentum ekle
            close_p *= (1 + r)
            high_p = max(high_p, close_p)
            low_p = min(low_p, close_p)

        # Wick ekle (daha gerçekçi mum gövdesi)
        wick = abs(close_p - open_p) * random.uniform(0.1, 0.4)
        if self._trend > 0:
            high_p = max(high_p, close_p + wick)
        else:
            low_p = min(low_p, close_p - wick)

        # Trend süresi dolunca flip
        self._trend_candles += 1
        if self._trend_candles >= self._trend_duration:
            self._trend *= -1
            self._trend_candles = 0
            self._trend_duration = random.randint(5, 20)
            self._momentum = 0.0
            # Kırılma anında volatilite artışı (breakout spike)
            self._volatility = random.uniform(0.0020, 0.0035)
            logger.debug(f"Trend flip → {'UP' if self._trend > 0 else 'DOWN'} | dur={self._trend_duration}")
        else:
            # Normal seyirde volatilite normalize
            self._volatility = max(0.0012, self._volatility * 0.95)

        self._price = close_p
        ts = int(time.time() * 1000)

        return Candle(
            open=round(open_p, 2),
            high=round(max(open_p, high_p), 2),
            low=round(min(open_p, low_p), 2),
            close=round(close_p, 2),
            volume=round(random.uniform(20, 200), 2),
            timestamp=ts,
        )
