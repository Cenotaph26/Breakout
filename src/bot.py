"""
TrendBreak v4 — Multi-coin scanner.
Tüm USDT-M Futures coinlerini 1dk mumlarla tarar.
Aynı anda 5 pozisyona kadar açar.
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
    symbol: str; side: str; entry: float; qty: float; sl: float
    size_usdt: float; leverage: int
    oid: int = 0; sl_oid: int = 0; opened: str = ""

    def pnl(self, price: float) -> float:
        d = (price - self.entry) / self.entry if self.side == "long" \
            else (self.entry - self.entry and (self.entry - price) / self.entry)
        if self.side == "long":
            d = (price - self.entry) / self.entry
        else:
            d = (self.entry - price) / self.entry
        return d * self.size_usdt * self.leverage


@dataclass
class SymbolState:
    """Tek bir coin'in durumu."""
    symbol: str
    candles: list  # son 80 mum
    price: float = 0.0
    trend: Optional[dict] = None
    live_candle: Optional[dict] = None


@dataclass
class Config:
    symbols: list = None  # None = tümünü tara
    period: int = 5
    threshold: float = 0.0005
    size_usdt: float = 20.0
    leverage: int = 5
    sl_pct: float = 0.005
    cooldown: float = 30.0
    max_positions: int = 5
    mode: str = "live"
    # Filtreler
    min_volume_usdt: float = 5_000_000  # 24h min volume
    top_n: int = 30  # en yüksek hacimli N coin

    def __post_init__(self):
        if self.symbols is None:
            self.symbols = []


class Bot:
    def __init__(self, cfg: Config, exc=None):
        self.cfg = cfg
        self.exc = exc
        self.running = False

        # Multi-coin state
        self.symbols: dict[str, SymbolState] = {}  # symbol -> state
        self.positions: dict[str, Position] = {}    # symbol -> position
        self.active_symbols: list[str] = []         # taranan semboller

        # Stats
        self.trades = 0; self.wins = 0; self.losses = 0; self.total_pnl = 0.0
        self.trade_log: list[dict] = []

        # Safety
        self._lock = asyncio.Lock()
        self._last_trade: dict[str, float] = {}  # symbol -> last trade time
        self._margin_set: set = set()
        self._filters: dict = {}  # symbol -> {qty_step, qty_prec, price_step, price_prec}

    # ── Log ───────────────────────────────────────────────

    def _log(self, tp, msg, pnl=0.0):
        ts = int(time.time() * 1000)
        if self.trade_log:
            ts = max(ts, self.trade_log[-1]["ts"] + 1)
        self.trade_log.append({
            "type": tp, "msg": msg, "pnl": round(pnl, 4),
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"), "ts": ts,
        })
        if len(self.trade_log) > 300:
            self.trade_log = self.trade_log[-300:]
        log.info(f"[{tp}] {msg}")

    # ── Trend ────────────────────────────────────────────

    def _analyze(self, sym: str):
        ss = self.symbols.get(sym)
        if not ss or len(ss.candles) < self.cfg.period:
            if ss: ss.trend = None
            return

        win = ss.candles[-self.cfg.period:]
        high = max(c["h"] for c in win)
        low = min(c["l"] for c in win)
        closes = [c["c"] for c in win]
        ups = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        downs = len(closes) - 1 - ups
        d = "up" if ups > downs else "down" if downs > ups else "sideways"
        ss.trend = {"dir": d, "high": high, "low": low,
                    "strength": round(max(ups, downs) / max(len(closes)-1, 1), 2)}

    def _signal(self, sym: str, price: float) -> Optional[str]:
        ss = self.symbols.get(sym)
        if not ss or not ss.trend:
            return None
        t = ss.trend
        thr = self.cfg.threshold
        if price > t["high"] * (1 + thr):
            return "long"
        if price < t["low"] * (1 - thr):
            return "short"
        return None

    def _check_exit(self, sym: str, price: float) -> Optional[str]:
        pos = self.positions.get(sym)
        if not pos:
            return None
        if pos.side == "long" and price <= pos.sl:
            return "STOP LOSS"
        if pos.side == "short" and price >= pos.sl:
            return "STOP LOSS"
        ss = self.symbols.get(sym)
        if ss and ss.trend:
            thr = self.cfg.threshold
            if pos.side == "long" and price < ss.trend["low"] * (1 - thr):
                return "TERS KIRILMA"
            if pos.side == "short" and price > ss.trend["high"] * (1 + thr):
                return "TERS KIRILMA"
        return None

    # ── Quantity ──────────────────────────────────────────

    def _qty(self, sym: str, price: float) -> float:
        f = self._filters.get(sym, {"qty_step": 0.001, "qty_prec": 3})
        step = f["qty_step"]; prec = f["qty_prec"]
        raw = self.cfg.size_usdt * self.cfg.leverage / price
        floored = math.floor(raw / step) * step
        return round(max(floored, step), prec)

    def _rqty(self, sym: str, qty: float) -> str:
        f = self._filters.get(sym, {"qty_step": 0.001, "qty_prec": 3})
        step = f["qty_step"]; prec = f["qty_prec"]
        v = math.floor(qty / step) * step
        return f"{round(v, prec):.{prec}f}"

    def _rprice(self, sym: str, price: float) -> str:
        f = self._filters.get(sym, {"price_step": 0.01, "price_prec": 2})
        step = f["price_step"]; prec = f["price_prec"]
        v = round(round(price / step) * step, prec)
        return f"{v:.{prec}f}"

    def _can_trade(self, sym: str) -> bool:
        last = self._last_trade.get(sym, 0)
        return time.time() - last >= self.cfg.cooldown

    # ── Open ──────────────────────────────────────────────

    async def _open(self, sym: str, side: str, price: float):
        if sym in self.positions:
            return
        if len(self.positions) >= self.cfg.max_positions:
            return
        if not self._can_trade(sym):
            return

        cfg = self.cfg
        sl = price * (1 - cfg.sl_pct) if side == "long" else price * (1 + cfg.sl_pct)
        qty = self._qty(sym, price)
        oid = 0; sl_oid = 0

        if self.exc and cfg.mode == "live":
            try:
                bal = await self.exc.get_balance()
                needed = cfg.size_usdt * 1.15
                if bal < needed:
                    self._log("error", f"{sym} bakiye yetersiz: {bal:.2f}")
                    self._last_trade[sym] = time.time()
                    return

                bp = await self.exc.get_position(sym)
                if bp and abs(float(bp.get("positionAmt", 0))) > 0:
                    self._log("error", f"{sym} zaten açık pozisyon")
                    self._last_trade[sym] = time.time()
                    return

                await self.exc.set_leverage(sym, cfg.leverage)
                if sym not in self._margin_set:
                    await self.exc.set_margin_type(sym, "ISOLATED")
                    self._margin_set.add(sym)

                os_ = "BUY" if side == "long" else "SELL"
                res = await self.exc.market_order(sym, os_, qty)
                oid = res.get("orderId", 0)

                avg = float(res.get("avgPrice") or 0)
                if avg > 0:
                    price = avg
                else:
                    await asyncio.sleep(0.3)
                    pi = await self.exc.get_position(sym)
                    if pi:
                        ep = float(pi.get("entryPrice", 0))
                        if ep > 0: price = ep

                sl = price * (1 - cfg.sl_pct) if side == "long" else price * (1 + cfg.sl_pct)
                sl_side = "SELL" if side == "long" else "BUY"
                sl_res = await self.exc.stop_order(sym, sl_side, qty, sl)
                sl_oid = sl_res.get("orderId", 0)
            except Exception as e:
                self._log("error", f"{sym} emir hatası: {e}")
                self._last_trade[sym] = time.time()
                return

        self._last_trade[sym] = time.time()
        self.positions[sym] = Position(
            symbol=sym, side=side, entry=price, qty=qty, sl=sl,
            size_usdt=cfg.size_usdt, leverage=cfg.leverage,
            oid=oid, sl_oid=sl_oid,
            opened=datetime.now(timezone.utc).strftime("%H:%M:%S"),
        )
        tag = "CANLI" if cfg.mode == "live" else "SIM"
        self._log(side, f"[{tag}] {sym} {side.upper()} @ {price:.2f} | qty={qty} | SL={sl:.2f}")

    async def _close(self, sym: str, price: float, reason: str):
        pos = self.positions.get(sym)
        if not pos:
            return

        if self.exc and self.cfg.mode == "live":
            try:
                await self.exc.cancel_all_orders(sym)
                bp = await self.exc.get_position(sym)
                amt = abs(float(bp["positionAmt"])) if bp else pos.qty
                if amt > 0:
                    signed = amt if pos.side == "long" else -amt
                    await self.exc.close_position(sym, signed)
            except Exception as e:
                self._log("error", f"{sym} kapatma hatası: {e}")

        pnl = pos.pnl(price)
        self.trades += 1
        if pnl > 0: self.wins += 1
        else: self.losses += 1
        self.total_pnl += pnl

        s = "+" if pnl >= 0 else ""
        self._log("close", f"{sym} KAPANDI ({reason}) @ {price:.2f} | PnL: {s}{pnl:.2f}$", pnl)
        del self.positions[sym]
        self._last_trade[sym] = time.time()

    # ── Events ────────────────────────────────────────────

    async def on_candle(self, sym: str, candle_data: dict):
        """Bir coin'de mum kapandığında."""
        async with self._lock:
            ss = self.symbols.get(sym)
            if not ss:
                ss = SymbolState(symbol=sym, candles=[])
                self.symbols[sym] = ss

            ss.candles.append(candle_data)
            if len(ss.candles) > 100:
                ss.candles = ss.candles[-100:]
            ss.price = candle_data["c"]
            ss.live_candle = None
            self._analyze(sym)

            if not ss.trend:
                return

            # Çıkış
            if sym in self.positions:
                reason = self._check_exit(sym, candle_data["c"])
                if reason:
                    await self._close(sym, candle_data["c"], reason)
                return

            # Giriş
            if sym not in self.positions and self._can_trade(sym):
                sig = self._signal(sym, candle_data["c"])
                if sig:
                    await self._open(sym, sig, candle_data["c"])

    async def on_tick(self, sym: str, price: float):
        """Fiyat tick — sadece SL kontrolü."""
        if not self.running:
            return
        ss = self.symbols.get(sym)
        if ss:
            ss.price = price

        pos = self.positions.get(sym)
        if pos and not self._lock.locked():
            hit = (pos.side == "long" and price <= pos.sl) or \
                  (pos.side == "short" and price >= pos.sl)
            if hit:
                async with self._lock:
                    if sym in self.positions:
                        await self._close(sym, price, "STOP LOSS (RT)")

    def update_live_candle(self, sym: str, data: dict):
        ss = self.symbols.get(sym)
        if ss:
            ss.live_candle = data
            ss.price = data["c"]

    # ── State ─────────────────────────────────────────────

    def state(self) -> dict:
        wr = round(self.wins / self.trades * 100, 1) if self.trades > 0 else 0

        # Pozisyonları listele
        pos_list = []
        for sym, pos in self.positions.items():
            ss = self.symbols.get(sym)
            p = ss.price if ss else pos.entry
            d = asdict(pos)
            d["pnl"] = round(pos.pnl(p), 4)
            d["current_price"] = p
            pos_list.append(d)

        # Seçili coin'in grafiği (ilk aktif sembol veya ilk pozisyon)
        chart_sym = None
        if self.positions:
            chart_sym = list(self.positions.keys())[0]
        elif self.active_symbols:
            chart_sym = self.active_symbols[0]

        chart_data = None
        chart_live = None
        chart_trend = None
        if chart_sym and chart_sym in self.symbols:
            ss = self.symbols[chart_sym]
            chart_data = ss.candles[-80:]
            chart_live = ss.live_candle
            chart_trend = ss.trend

        return {
            "running": self.running,
            "mode": self.cfg.mode,
            "active_symbols": self.active_symbols[:30],
            "positions": pos_list,
            "max_positions": self.cfg.max_positions,
            "chart_sym": chart_sym,
            "candles": chart_data,
            "live_candle": chart_live,
            "trend": chart_trend,
            "stats": {"trades": self.trades, "wins": self.wins, "losses": self.losses,
                      "pnl": round(self.total_pnl, 4), "wr": wr},
            "log": self.trade_log[-60:],
            "cfg": asdict(self.cfg),
            "prices": {s: ss.price for s, ss in self.symbols.items() if ss.price > 0},
        }
