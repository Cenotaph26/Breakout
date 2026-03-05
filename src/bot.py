"""
TrendBreak Bot — Core Strategy Engine
1-Minute Trend Following + Breakout Entry/Exit
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


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
    side: str          # 'long' | 'short'
    entry_price: float
    size_usdt: float
    leverage: int
    stop_loss: float
    open_time: str
    symbol: str

    def unrealized_pnl(self, current_price: float) -> float:
        if self.side == "long":
            pct = (current_price - self.entry_price) / self.entry_price
        else:
            pct = (self.entry_price - current_price) / self.entry_price
        return pct * self.size_usdt * self.leverage

    def to_dict(self, current_price: float) -> dict:
        d = asdict(self)
        d["unrealized_pnl"] = round(self.unrealized_pnl(current_price), 4)
        d["current_price"] = current_price
        return d


@dataclass
class TrendAnalysis:
    direction: str      # 'up' | 'down' | 'sideways'
    trend_high: float
    trend_low: float
    up_candles: int
    down_candles: int
    strength: float     # 0-1


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
    symbol: str = "BTCUSDT"
    trend_period: int = 5          # number of 1m candles for trend
    break_threshold: float = 0.0005  # 0.05% breakout confirmation
    trade_size_usdt: float = 100.0
    leverage: int = 5
    stop_loss_pct: float = 0.005   # 0.5%
    max_candles: int = 200
    mode: str = "sim"              # 'sim' | 'live'


class TrendBreakBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.candles: list[Candle] = []
        self.position: Optional[Position] = None
        self.stats = BotStats()
        self.running = False
        self.current_price = 0.0
        self.trade_log: list[dict] = []
        self.current_trend: Optional[TrendAnalysis] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Trend Analysis
    # ------------------------------------------------------------------
    def analyze_trend(self) -> Optional[TrendAnalysis]:
        n = self.config.trend_period
        if len(self.candles) < n:
            return None

        recent = self.candles[-n:]
        trend_high = max(c.high for c in recent)
        trend_low = min(c.low for c in recent)

        up = sum(1 for i in range(1, len(recent)) if recent[i].close > recent[i-1].close)
        down = len(recent) - 1 - up

        if up > down * 1.5:
            direction = "up"
        elif down > up * 1.5:
            direction = "down"
        else:
            direction = "sideways"

        strength = max(up, down) / max((len(recent) - 1), 1)

        return TrendAnalysis(
            direction=direction,
            trend_high=trend_high,
            trend_low=trend_low,
            up_candles=up,
            down_candles=down,
            strength=round(strength, 2),
        )

    # ------------------------------------------------------------------
    # Signal Detection
    # ------------------------------------------------------------------
    def detect_breakout(self, trend: TrendAnalysis, price: float) -> Optional[str]:
        thr = self.config.break_threshold
        if price > trend.trend_high * (1 + thr):
            return "long"
        if price < trend.trend_low * (1 - thr):
            return "short"
        return None

    def check_exit(self, trend: TrendAnalysis, price: float) -> Optional[str]:
        """Returns exit reason string or None."""
        pos = self.position
        if not pos:
            return None

        # Stop loss
        if pos.side == "long" and price <= pos.stop_loss:
            return "STOP LOSS"
        if pos.side == "short" and price >= pos.stop_loss:
            return "STOP LOSS"

        # Trend reversal exit
        if pos.side == "long" and price < trend.trend_low:
            return "TREND BOZULDU"
        if pos.side == "short" and price > trend.trend_high:
            return "TREND BOZULDU"

        return None

    # ------------------------------------------------------------------
    # Trade execution
    # ------------------------------------------------------------------
    async def open_position(self, side: str, price: float):
        cfg = self.config
        sl = price * (1 - cfg.stop_loss_pct) if side == "long" else price * (1 + cfg.stop_loss_pct)

        self.position = Position(
            side=side,
            entry_price=price,
            size_usdt=cfg.trade_size_usdt,
            leverage=cfg.leverage,
            stop_loss=sl,
            open_time=datetime.utcnow().isoformat(),
            symbol=cfg.symbol,
        )

        msg = f"{side.upper()} açıldı @ {price:.4f} | SL: {sl:.4f} | {cfg.trade_size_usdt}$ × {cfg.leverage}x"
        logger.info(msg)
        self._log_trade(side, msg)

    async def close_position(self, price: float, reason: str):
        pos = self.position
        if not pos:
            return

        pnl = pos.unrealized_pnl(price)
        pct = pnl / (pos.size_usdt * pos.leverage) * 100

        self.stats.total_trades += 1
        if pnl > 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.stats.total_pnl += pnl
        self.stats.last_trade_time = datetime.utcnow().isoformat()

        msg = f"KAPANDI ({reason}) @ {price:.4f} | PnL: {pnl:+.4f}$ ({pct:+.2f}%)"
        logger.info(msg)
        self._log_trade("close", msg, pnl=pnl)

        self.position = None

    def _log_trade(self, type_: str, msg: str, pnl: float = 0.0):
        self.trade_log.append({
            "type": type_,
            "msg": msg,
            "pnl": round(pnl, 4),
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "ts": int(time.time() * 1000),
        })
        if len(self.trade_log) > 200:
            self.trade_log = self.trade_log[-200:]

    # ------------------------------------------------------------------
    # Core tick — called with each new candle or price update
    # ------------------------------------------------------------------
    async def on_new_candle(self, candle: Candle):
        async with self._lock:
            self.candles.append(candle)
            if len(self.candles) > self.config.max_candles:
                self.candles.pop(0)

            self.current_price = candle.close
            trend = self.analyze_trend()
            self.current_trend = trend

            if not trend:
                return

            # Exit check
            if self.position:
                reason = self.check_exit(trend, candle.close)
                if reason:
                    await self.close_position(candle.close, reason)
                    return

            # Entry check
            if not self.position and trend.direction != "sideways":
                signal = self.detect_breakout(trend, candle.close)
                if signal:
                    await self.open_position(signal, candle.close)

    # ------------------------------------------------------------------
    # State snapshot for API
    # ------------------------------------------------------------------
    def get_state(self) -> dict:
        trend_dict = None
        if self.current_trend:
            trend_dict = asdict(self.current_trend)

        position_dict = None
        if self.position:
            position_dict = self.position.to_dict(self.current_price)

        candles_out = []
        for c in self.candles[-80:]:
            candles_out.append({
                "o": c.open, "h": c.high,
                "l": c.low,  "c": c.close,
                "t": c.timestamp,
            })

        wr = 0
        if self.stats.total_trades > 0:
            wr = round(self.stats.wins / self.stats.total_trades * 100, 1)

        return {
            "running": self.running,
            "current_price": self.current_price,
            "trend": trend_dict,
            "position": position_dict,
            "candles": candles_out,
            "stats": {
                **asdict(self.stats),
                "win_rate": wr,
            },
            "trade_log": self.trade_log[-50:],
            "config": asdict(self.config),
        }
