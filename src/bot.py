"""
TrendBreak Bot — Core Strategy Engine
Binance USDT-M Futures üzerinde 1dk mum trend takibi + breakout.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: int  # ms


@dataclass
class Position:
    side: str
    entry_price: float
    size_usdt: float
    leverage: int
    stop_loss: float
    open_time: str
    symbol: str
    order_id: int = 0
    stop_order_id: int = 0
    quantity: float = 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "long":
            pct = (current_price - self.entry_price) / self.entry_price
        else:
            pct = (self.entry_price - current_price) / self.entry_price
        return pct * self.size_usdt * self.leverage

    def to_dict(self, current_price: float) -> dict:
        d = asdict(self)
        d["unrealized_pnl"] = round(self.unrealized_pnl(current_price), 4)
        d["current_price"]  = current_price
        return d


@dataclass
class TrendAnalysis:
    direction: str
    trend_high: float
    trend_low: float
    up_candles: int
    down_candles: int
    strength: float
    support: float = 0.0
    resistance: float = 0.0


@dataclass
class BotStats:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    start_time: str = ""
    last_trade_time: str = ""


@dataclass
class BotConfig:
    symbol: str          = "BTCUSDT"
    trend_period: int    = 4
    break_threshold: float = 0.0005
    trade_size_usdt: float = 100.0
    leverage: int        = 5
    stop_loss_pct: float = 0.005
    max_candles: int     = 200
    mode: str            = "sim"


class TrendBreakBot:
    def __init__(self, config: BotConfig, executor=None):
        self.config   = config
        self.executor = executor
        self.candles: list[Candle] = []
        self.position: Optional[Position] = None
        self.stats    = BotStats(start_time=datetime.utcnow().isoformat())
        self.running  = False
        self.current_price = 0.0
        self.trade_log: list[dict] = []
        self.current_trend: Optional[TrendAnalysis] = None
        self._lock     = asyncio.Lock()
        self._min_qty  = 0.001
        self._precision = 3

        # Cooldown & safety
        self._last_trade_time: float = 0.0
        self._cooldown_secs: float = 10.0
        self._margin_type_set: set = set()

    def _can_trade(self) -> bool:
        return (time.time() - self._last_trade_time) >= self._cooldown_secs

    # ── Trend analysis ─────────────────────────────────────────────────────

    def analyze_trend(self) -> Optional[TrendAnalysis]:
        n = self.config.trend_period
        if len(self.candles) < n + 1:
            return None

        channel    = self.candles[-(n+1):-1]
        trend_high = max(c.high  for c in channel)
        trend_low  = min(c.low   for c in channel)

        closes = [c.close for c in channel]
        up   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        down = len(closes) - 1 - up

        if up >= down + 2:        direction = "up"
        elif down >= up + 2:      direction = "down"
        else:                     direction = "sideways"

        return TrendAnalysis(
            direction    = direction,
            trend_high   = trend_high,
            trend_low    = trend_low,
            up_candles   = up,
            down_candles = down,
            strength     = round(max(up, down) / max(len(closes) - 1, 1), 2),
            support      = trend_low,
            resistance   = trend_high,
        )

    def detect_breakout(self, trend: TrendAnalysis, price: float) -> Optional[str]:
        thr = self.config.break_threshold

        above = price > trend.trend_high * (1 + thr)
        below = price < trend.trend_low  * (1 - thr)

        # Trend yönüne uyumlu breakout
        if above and trend.direction != "down":
            return "long"
        if below and trend.direction != "up":
            return "short"
        # Counter-trend: 2x threshold gerekli
        if above and price > trend.trend_high * (1 + thr * 2):
            return "long"
        if below and price < trend.trend_low * (1 - thr * 2):
            return "short"

        return None

    def check_exit(self, trend: TrendAnalysis, price: float) -> Optional[str]:
        pos = self.position
        if not pos:
            return None

        if pos.side == "long"  and price <= pos.stop_loss:
            return "STOP LOSS"
        if pos.side == "short" and price >= pos.stop_loss:
            return "STOP LOSS"

        if pos.side == "long"  and trend.direction == "down" and price < trend.trend_low:
            return "TREND REVERSAL"
        if pos.side == "short" and trend.direction == "up"   and price > trend.trend_high:
            return "TREND REVERSAL"

        return None

    # ── Position sizing ───────────────────────────────────────────────────

    def _calc_quantity(self, price: float) -> float:
        """Notional → base qty. FLOOR yuvarlama (margin aşımını önler)."""
        step = self.executor._qty_step if self.executor else self._min_qty
        prec = self.executor._qty_prec if self.executor else 3

        notional = self.config.trade_size_usdt * self.config.leverage
        qty = notional / price

        # FLOOR — asla yukarı yuvarlama
        qty_rounded = math.floor(qty / step) * step
        qty_rounded = round(qty_rounded, prec)

        if qty_rounded < step:
            qty_rounded = step

        return qty_rounded

    # ── Order execution ──────────────────────────────────────────────────

    async def open_position(self, side: str, price: float):
        if not self._can_trade():
            return

        cfg      = self.config
        sl_price = price * (1 - cfg.stop_loss_pct) if side == "long" else price * (1 + cfg.stop_loss_pct)
        quantity = self._calc_quantity(price)

        order_id      = 0
        stop_order_id = 0

        if self.executor and cfg.mode == "live":
            try:
                # Bakiye kontrolü
                balance = await self.executor.get_balance()
                margin_needed = cfg.trade_size_usdt * 1.1
                if balance < margin_needed:
                    msg = f"Yetersiz bakiye: {balance:.2f} < {margin_needed:.2f} USDT"
                    logger.warning(msg)
                    self._log_trade("error", msg)
                    self._last_trade_time = time.time()
                    return

                # Mevcut pozisyon kontrolü
                existing = await self.executor.get_position(cfg.symbol)
                if existing and abs(float(existing.get("positionAmt", 0))) > 0:
                    logger.warning("Binance'de zaten açık pozisyon var")
                    self._log_trade("error", "Binance'de açık pozisyon mevcut")
                    self._last_trade_time = time.time()
                    return

                # Kaldıraç
                await self.executor.set_leverage(cfg.symbol, cfg.leverage)

                # Margin type — sadece 1 kez
                if cfg.symbol not in self._margin_type_set:
                    await self.executor.set_margin_type(cfg.symbol, "ISOLATED")
                    self._margin_type_set.add(cfg.symbol)

                # Market emir
                order_side = "BUY" if side == "long" else "SELL"
                result     = await self.executor.market_order(cfg.symbol, order_side, quantity)
                order_id   = result.get("orderId", 0)
                avg = float(result.get("avgPrice") or 0)
                if avg > 0:
                    price    = avg
                    sl_price = price * (1 - cfg.stop_loss_pct) if side == "long" else price * (1 + cfg.stop_loss_pct)
                else:
                    # Testnet avgPrice=0 döner, pozisyondan gerçek entry'yi al
                    import asyncio as _aio
                    await _aio.sleep(0.3)
                    pos_info = await self.executor.get_position(cfg.symbol)
                    if pos_info:
                        ep = float(pos_info.get("entryPrice", 0))
                        if ep > 0:
                            price = ep
                            sl_price = price * (1 - cfg.stop_loss_pct) if side == "long" else price * (1 + cfg.stop_loss_pct)
                            logger.info(f"Entry price from position: {price}")

                # Stop loss
                sl_side   = "SELL" if side == "long" else "BUY"
                sl_result = await self.executor.stop_market_order(cfg.symbol, sl_side, quantity, sl_price)
                stop_order_id = sl_result.get("orderId", 0)

            except Exception as e:
                logger.error(f"Emir açma hatası: {e}")
                self._log_trade("error", f"Emir hatası: {str(e)[:120]}")
                self._last_trade_time = time.time()
                return

        self._last_trade_time = time.time()

        self.position = Position(
            side=side, entry_price=price, size_usdt=cfg.trade_size_usdt,
            leverage=cfg.leverage, stop_loss=sl_price,
            open_time=datetime.utcnow().isoformat(), symbol=cfg.symbol,
            order_id=order_id, stop_order_id=stop_order_id, quantity=quantity,
        )

        tag = "CANLI" if cfg.mode == "live" else "SIM"
        msg = (f"[{tag}] {side.upper()} @ {price:.2f} "
               f"| qty={quantity} | SL={sl_price:.2f} "
               f"| {cfg.trade_size_usdt}$ x{cfg.leverage}")
        logger.info(msg)
        self._log_trade(side, msg)

    async def close_position(self, price: float, reason: str):
        pos = self.position
        if not pos:
            return

        if self.executor and self.config.mode == "live":
            try:
                await self.executor.cancel_all_orders(self.config.symbol)
                live_pos = await self.executor.get_position(self.config.symbol)
                qty = abs(float(live_pos["positionAmt"])) if live_pos else pos.quantity
                if qty > 0:
                    amt = qty if pos.side == "long" else -qty
                    await self.executor.close_position_market(self.config.symbol, amt)
            except Exception as e:
                logger.error(f"Pozisyon kapatma hatası: {e}")
                self._log_trade("error", f"Kapatma hatası: {str(e)[:120]}")

        pnl = pos.unrealized_pnl(price)
        pct = pnl / max(pos.size_usdt, 1) * 100

        self.stats.total_trades += 1
        if pnl > 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.stats.total_pnl       += pnl
        self.stats.last_trade_time  = datetime.utcnow().isoformat()

        sign = "+" if pnl > 0 else ""
        msg = f"KAPANDI ({reason}) @ {price:.2f} | PnL: {sign}{pnl:.2f}$ ({pct:+.1f}%)"
        logger.info(msg)
        self._log_trade("close", msg, pnl=pnl)

        self.position = None
        self._last_trade_time = time.time()

    # ── Log helper ────────────────────────────────────────────────────────

    def _log_trade(self, type_: str, msg: str, pnl: float = 0.0):
        ts = int(time.time() * 1000)
        if self.trade_log:
            ts = max(ts, self.trade_log[-1]["ts"] + 1)
        self.trade_log.append({
            "type": type_, "msg": msg, "pnl": round(pnl, 4),
            "time": datetime.utcnow().strftime("%H:%M:%S"), "ts": ts,
        })
        if len(self.trade_log) > 200:
            self.trade_log = self.trade_log[-200:]

    # ── Main candle handler ───────────────────────────────────────────────

    async def on_new_candle(self, candle: Candle):
        async with self._lock:
            self.candles.append(candle)
            if len(self.candles) > self.config.max_candles:
                self.candles.pop(0)

            self.current_price = candle.close
            trend              = self.analyze_trend()
            self.current_trend = trend

            if not trend:
                return

            if self.position:
                reason = self.check_exit(trend, candle.close)
                if reason:
                    await self.close_position(candle.close, reason)
                return

            if not self.position and self._can_trade():
                signal = self.detect_breakout(trend, candle.close)
                if signal:
                    logger.info(f"BREAKOUT: {signal} @ {candle.close:.2f} | H={trend.trend_high:.2f} L={trend.trend_low:.2f}")
                    await self.open_position(signal, candle.close)

    async def on_price_tick(self, price: float):
        """Gerçek zamanlı: SADECE stop-loss kontrolü. Yeni emir açmaz."""
        if not self.running:
            return
        self.current_price = price

        if self.position and not self._lock.locked():
            hit_sl = False
            if self.position.side == "long"  and price <= self.position.stop_loss:
                hit_sl = True
            elif self.position.side == "short" and price >= self.position.stop_loss:
                hit_sl = True

            if hit_sl:
                async with self._lock:
                    if self.position:
                        await self.close_position(price, "STOP LOSS (RT)")

    # ── State snapshot ────────────────────────────────────────────────────

    def get_state(self) -> dict:
        wr = 0
        if self.stats.total_trades > 0:
            wr = round(self.stats.wins / self.stats.total_trades * 100, 1)

        cd = max(0, self._cooldown_secs - (time.time() - self._last_trade_time))

        return {
            "running":       self.running,
            "mode":          self.config.mode,
            "current_price": self.current_price,
            "trend":         asdict(self.current_trend) if self.current_trend else None,
            "position":      self.position.to_dict(self.current_price) if self.position else None,
            "candles":       [{"o":c.open,"h":c.high,"l":c.low,"c":c.close,"t":c.timestamp}
                              for c in self.candles[-80:]],
            "stats":         {**asdict(self.stats), "win_rate": wr},
            "trade_log":     self.trade_log[-50:],
            "config":        asdict(self.config),
            "cooldown":      round(cd, 1),
        }
