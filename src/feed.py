"""Multi-coin Feed — Binance combined WS streams."""

import asyncio, json, logging, random, time
import websockets
from src.bot import Bot, SymbolState

log = logging.getLogger("feed")
TESTNET_WS = "wss://fstream.binancefuture.com/ws"
LIVE_WS = "wss://fstream.binance.com/ws"


class MultiFeed:
    """Birden fazla coin'i tek veya birkaç WS bağlantısıyla dinler."""

    def __init__(self, bot: Bot, symbols: list[str], demo=True):
        self.bot = bot
        self.symbols = symbols
        self.demo = demo
        self._tasks = []

    def _ws(self):
        return TESTNET_WS if self.demo else LIVE_WS

    async def start(self):
        # Binance combined stream: max 200 stream per connection
        # kline + miniTicker per symbol = 2 stream → max 100 symbol per conn
        chunks = [self.symbols[i:i+50] for i in range(0, len(self.symbols), 50)]
        for chunk in chunks:
            self._tasks.append(asyncio.create_task(self._combined_loop(chunk)))
        log.info(f"MultiFeed started: {len(self.symbols)} coins, {len(self._tasks)} WS connections")

    async def stop(self):
        for t in self._tasks:
            t.cancel()
            try: await t
            except: pass
        self._tasks = []

    async def _combined_loop(self, symbols: list[str]):
        """Bir grup coin için combined stream."""
        streams = []
        for sym in symbols:
            s = sym.lower()
            streams.append(f"{s}@kline_1m")
            streams.append(f"{s}@miniTicker")

        base = self._ws()
        url = f"{base}/stream?streams=" + "/".join(streams)
        log.info(f"Combined WS → {len(symbols)} coins ({len(streams)} streams)")

        while self.bot.running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=15, close_timeout=5) as ws:
                    log.info(f"Combined WS bağlandı ({len(symbols)} coins)")
                    async for raw in ws:
                        if not self.bot.running:
                            break
                        try:
                            msg = json.loads(raw)
                            data = msg.get("data", {})
                            stream = msg.get("stream", "")

                            if "@kline_" in stream:
                                k = data.get("k", {})
                                if not k: continue
                                sym = k.get("s", "").upper()
                                price = float(k.get("c", 0))

                                # Live candle güncelle
                                self.bot.update_live_candle(sym, {
                                    "o": float(k["o"]), "h": float(k["h"]),
                                    "l": float(k["l"]), "c": price, "t": int(k["t"]),
                                })

                                # Mum kapandıysa
                                if k.get("x"):
                                    cd = {"o": float(k["o"]), "h": float(k["h"]),
                                          "l": float(k["l"]), "c": float(k["c"]),
                                          "v": float(k["v"]), "t": int(k["t"])}
                                    await self.bot.on_candle(sym, cd)

                            elif "@miniTicker" in stream:
                                sym = data.get("s", "").upper()
                                if "c" in data:
                                    await self.bot.on_tick(sym, float(data["c"]))

                        except Exception as e:
                            log.debug(f"Parse: {e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Combined WS hata: {e}")
                await asyncio.sleep(5)


class SimFeed:
    def __init__(self, bot: Bot, interval=2.0):
        self.bot = bot; self.interval = interval; self._task = None
        self._prices = {"BTCUSDT": 67000, "ETHUSDT": 3400, "SOLUSDT": 140,
                        "BNBUSDT": 580, "XRPUSDT": 0.55}
        self._trends = {s: random.choice([-1, 1]) for s in self._prices}
        self._tc = {s: 0 for s in self._prices}

    async def start(self):
        self.bot.active_symbols = list(self._prices.keys())
        for sym in self._prices:
            self.bot.symbols[sym] = SymbolState(symbol=sym, candles=[])
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task: self._task.cancel()
        try: await self._task
        except: pass

    async def _run(self):
        while self.bot.running:
            for sym, p in list(self._prices.items()):
                t = self._trends[sym]
                bias = 0.55 if t > 0 else 0.45
                mom = t * 0.0003
                cp = p
                hp = lp = p
                for _ in range(10):
                    r = (random.random() - (1-bias)) * 0.002 + mom
                    cp *= (1+r); hp = max(hp,cp); lp = min(lp,cp)
                cd = {"o":round(p,2),"h":round(max(p,hp),2),"l":round(min(p,lp),2),
                      "c":round(cp,2),"v":round(random.uniform(20,200),2),"t":int(time.time()*1000)}
                self._prices[sym] = cp
                self._tc[sym] += 1
                if self._tc[sym] > random.randint(5,15):
                    self._trends[sym] *= -1; self._tc[sym] = 0
                self.bot.update_live_candle(sym, cd)
                await self.bot.on_candle(sym, cd)
            await asyncio.sleep(self.interval)
