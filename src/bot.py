"""
TrendBreak Bot v3 — Her adımda log, sade mantık.
"""

import asyncio, logging, math, time
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime, timezone

log = logging.getLogger("bot")


@dataclass
class Candle:
    o: float; h: float; l: float; c: float; v: float; t: int


@dataclass
class Position:
    side: str; entry: float; qty: float; sl: float
    size_usdt: float; leverage: int; symbol: str
    oid: int = 0; sl_oid: int = 0

    def pnl(self, price: float) -> float:
        if self.side == "long":
            return (price - self.entry) / self.entry * self.size_usdt * self.leverage
        else:
            return (self.entry - price) / self.entry * self.size_usdt * self.leverage

    def as_dict(self, price: float) -> dict:
        d = asdict(self)
        d["pnl"] = round(self.pnl(price), 4)
        return d


@dataclass
class Config:
    symbol: str = "BTCUSDT"
    period: int = 5
    threshold: float = 0.0005
    size_usdt: float = 100.0
    leverage: int = 5
    sl_pct: float = 0.005
    cooldown: float = 15.0
    max_candles: int = 200
    mode: str = "sim"


class Bot:
    def __init__(self, cfg: Config, exc=None):
        self.cfg = cfg
        self.exc = exc
        self.candles: list[Candle] = []
        self.pos: Optional[Position] = None
        self.price: float = 0.0
        self.running = False
        self.live_candle: Optional[dict] = None

        self.trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self.trade_log: list[dict] = []
        self.trend: Optional[dict] = None

        self._lock = asyncio.Lock()
        self._last_trade: float = 0.0
        self._margin_set = False

    # ── Logging ──────────────────────────────────────────

    def _ts(self):
        return datetime.now(timezone.utc).strftime("%H:%M:%S")

    def _log(self, tp, msg, pnl=0.0):
        ts = int(time.time() * 1000)
        if self.trade_log:
            ts = max(ts, self.trade_log[-1]["ts"] + 1)
        entry = {"type": tp, "msg": msg, "pnl": round(pnl, 4), "time": self._ts(), "ts": ts}
        self.trade_log.append(entry)
        if len(self.trade_log) > 200:
            self.trade_log = self.trade_log[-200:]
        log.info(f"[{tp}] {msg}")

    # ── Trend ────────────────────────────────────────────

    def _update_trend(self):
        n = self.cfg.period
        if len(self.candles) < n:
            self.trend = None
            return

        win = self.candles[-n:]
        high = max(c.h for c in win)
        low = min(c.l for c in win)

        closes = [c.c for c in win]
        ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i - 1])
        downs = len(closes) - 1 - ups

        if ups > downs:
            d = "up"
        elif downs > ups:
            d = "down"
        else:
            d = "sideways"

        self.trend = {"dir": d, "high": high, "low": low, "strength": round(max(ups, downs) / max(len(closes) - 1, 1), 2)}

    def _check_signal(self, price: float) -> Optional[str]:
        if not self.trend:
            return None
        thr = self.cfg.threshold
        if price > self.trend["high"] * (1 + thr):
            return "long"
        if price < self.trend["low"] * (1 - thr):
            return "short"
        return None

    def _check_exit(self, price: float) -> Optional[str]:
        if not self.pos:
            return None
        if self.pos.side == "long" and price <= self.pos.sl:
            return "STOP LOSS"
        if self.pos.side == "short" and price >= self.pos.sl:
            return "STOP LOSS"
        # Ters sinyal
        if self.trend:
            thr = self.cfg.threshold
            if self.pos.side == "long" and price < self.trend["low"] * (1 - thr):
                return "TERS KIRILMA"
            if self.pos.side == "short" and price > self.trend["high"] * (1 + thr):
                return "TERS KIRILMA"
        return None

    # ── Quantity ─────────────────────────────────────────

    def _calc_qty(self, price: float) -> float:
        step = getattr(self.exc, '_qty_step', 0.001) if self.exc else 0.001
        prec = getattr(self.exc, '_qty_prec', 3) if self.exc else 3
        raw = self.cfg.size_usdt * self.cfg.leverage / price
        floored = math.floor(raw / step) * step
        return round(max(floored, step), prec)

    def _can_trade(self) -> bool:
        return time.time() - self._last_trade >= self.cfg.cooldown

    # ── Open ─────────────────────────────────────────────

    async def _open(self, side: str, price: float):
        if self.pos:
            log.debug("_open atlandı: zaten pozisyon var")
            return
        if not self._can_trade():
            log.debug(f"_open atlandı: cooldown ({self.cfg.cooldown - (time.time() - self._last_trade):.0f}s kaldı)")
            return

        cfg = self.cfg
        sl = price * (1 - cfg.sl_pct) if side == "long" else price * (1 + cfg.sl_pct)
        qty = self._calc_qty(price)
        oid = 0
        sl_oid = 0

        if self.exc and cfg.mode == "live":
            try:
                # Bakiye
                bal = await self.exc.get_balance()
                needed = cfg.size_usdt * 1.15
                if bal < needed:
                    self._log("error", f"Yetersiz bakiye: {bal:.2f} < {needed:.2f} USDT")
                    self._last_trade = time.time()
                    return

                # Binance pozisyon kontrolü
                bp = await self.exc.get_position(cfg.symbol)
                if bp and abs(float(bp.get("positionAmt", 0))) > 0:
                    self._log("error", "Binance'de zaten açık pozisyon var")
                    self._last_trade = time.time()
                    return

                # Kaldıraç
                await self.exc.set_leverage(cfg.symbol, cfg.leverage)

                # Margin type
                if not self._margin_set:
                    await self.exc.set_margin_type(cfg.symbol, "ISOLATED")
                    self._margin_set = True

                # Market emir
                order_side = "BUY" if side == "long" else "SELL"
                res = await self.exc.market_order(cfg.symbol, order_side, qty)
                oid = res.get("orderId", 0)

                # Entry fiyatı
                avg = float(res.get("avgPrice") or 0)
                if avg > 0:
                    price = avg
                else:
                    await asyncio.sleep(0.5)
                    pi = await self.exc.get_position(cfg.symbol)
                    if pi:
                        ep = float(pi.get("entryPrice", 0))
                        if ep > 0:
                            price = ep

                sl = price * (1 - cfg.sl_pct) if side == "long" else price * (1 + cfg.sl_pct)

                # Stop emri
                sl_side = "SELL" if side == "long" else "BUY"
                sl_res = await self.exc.stop_order(cfg.symbol, sl_side, qty, sl)
                sl_oid = sl_res.get("orderId", 0)

            except Exception as e:
                self._log("error", f"Emir hatası: {e}")
                self._last_trade = time.time()
                return

        self._last_trade = time.time()
        self.pos = Position(side=side, entry=price, qty=qty, sl=sl, size_usdt=cfg.size_usdt, leverage=cfg.leverage, symbol=cfg.symbol, oid=oid, sl_oid=sl_oid)

        tag = "CANLI" if cfg.mode == "live" else "SIM"
        self._log(side, f"[{tag}] {side.upper()} @ {price:.2f} | qty={qty} | SL={sl:.2f} | {cfg.size_usdt}$ x{cfg.leverage}")

    # ── Close ────────────────────────────────────────────

    async def _close(self, price: float, reason: str):
        if not self.pos:
            return

        if self.exc and self.cfg.mode == "live":
            try:
                await self.exc.cancel_all_orders(self.cfg.symbol)
                bp = await self.exc.get_position(self.cfg.symbol)
                amt = abs(float(bp["positionAmt"])) if bp else self.pos.qty
                if amt > 0:
                    signed = amt if self.pos.side == "long" else -amt
                    await self.exc.close_position(self.cfg.symbol, signed)
            except Exception as e:
                self._log("error", f"Kapatma hatası: {e}")

        pnl = self.pos.pnl(price)
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl += pnl

        s = "+" if pnl >= 0 else ""
        self._log("close", f"KAPANDI ({reason}) @ {price:.2f} | PnL: {s}{pnl:.2f}$", pnl)
        self.pos = None
        self._last_trade = time.time()

    # ── Events ───────────────────────────────────────────

    async def on_candle(self, candle: Candle):
        """Her mum kapanışında çağrılır."""
        async with self._lock:
            self.candles.append(candle)
            if len(self.candles) > self.cfg.max_candles:
                self.candles.pop(0)
            self.price = candle.c
            self._update_trend()

            if not self.trend:
                log.debug(f"Trend yok (mumlar: {len(self.candles)}/{self.cfg.period})")
                return

            # Pozisyon varsa: çıkış kontrolü
            if self.pos:
                reason = self._check_exit(candle.c)
                if reason:
                    await self._close(candle.c, reason)
                return  # Bu mumda yeni pozisyon açma

            # Pozisyon yoksa: giriş kontrolü
            sig = self._check_signal(candle.c)
            if sig:
                log.info(f"SİNYAL: {sig} | price={candle.c:.2f} H={self.trend['high']:.2f} L={self.trend['low']:.2f}")
                await self._open(sig, candle.c)
            else:
                log.debug(f"Sinyal yok | price={candle.c:.2f} H={self.trend['high']:.2f} L={self.trend['low']:.2f} thr={self.cfg.threshold}")

    async def on_tick(self, price: float):
        """Her fiyat tick'inde: SADECE SL kontrolü."""
        if not self.running:
            return
        self.price = price
        if self.pos and not self._lock.locked():
            hit = (self.pos.side == "long" and price <= self.pos.sl) or \
                  (self.pos.side == "short" and price >= self.pos.sl)
            if hit:
                async with self._lock:
                    if self.pos:
                        await self._close(price, "STOP LOSS (RT)")

    # ── State ────────────────────────────────────────────

    def state(self) -> dict:
        wr = round(self.wins / self.trades * 100, 1) if self.trades > 0 else 0
        cd = max(0, self.cfg.cooldown - (time.time() - self._last_trade))
        return {
            "running": self.running,
            "mode": self.cfg.mode,
            "price": self.price,
            "trend": self.trend,
            "pos": self.pos.as_dict(self.price) if self.pos else None,
            "candles": [{"o": c.o, "h": c.h, "l": c.l, "c": c.c, "t": c.t} for c in self.candles[-80:]],
            "live_candle": self.live_candle,
            "stats": {"trades": self.trades, "wins": self.wins, "losses": self.losses, "pnl": round(self.total_pnl, 4), "wr": wr},
            "log": self.trade_log[-50:],
            "cfg": asdict(self.cfg),
            "cooldown": round(cd, 1),
        }
