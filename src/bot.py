"""
TrendBreak Bot — Core Strategy Engine
Binance Demo (Testnet) üzerinde gerçek emir açar.
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
    side: str           # 'long' | 'short'
    entry_price: float
    size_usdt: float
    leverage: int
    stop_loss: float
    open_time: str
    symbol: str
    order_id: int = 0
    stop_order_id: int = 0
    quantity: float = 0.0  # base asset qty (örn. 0.001 BTC)

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
    symbol: str          = "BTCUSDT"
    trend_period: int    = 5
    break_threshold: float = 0.0005   # 0.05%
    trade_size_usdt: float = 100.0
    leverage: int        = 5
    stop_loss_pct: float = 0.005      # 0.5%
    max_candles: int     = 200
    mode: str            = "sim"      # 'sim' | 'live'


# ── Bot ───────────────────────────────────────────────────────────────────────

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
        self._precision = 3      # qty ondalık basamak

    # ── Trend analysis ────────────────────────────────────────────────────────

    def analyze_trend(self) -> Optional[TrendAnalysis]:
        n = self.config.trend_period
        if len(self.candles) < n + 1:
            return None

        channel    = self.candles[-(n+1):-1]   # son n mum (henüz kapanmamış hariç)
        trend_high = max(c.high  for c in channel)
        trend_low  = min(c.low   for c in channel)

        closes = [c.close for c in channel]
        up   = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
        down = len(closes) - 1 - up

        # Daha hassas trend tespiti
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
        )

    def detect_breakout(self, trend: TrendAnalysis, price: float) -> Optional[str]:
        """
        Breakout mantığı:
        - Her yönde: fiyat kanalın üstünü kırarsa LONG, altını kırarsa SHORT
        - Eşik: break_threshold (varsayılan 0.05%)
        - Sideways dahil tüm trend durumlarında çalışır
        """
        thr = self.config.break_threshold
        if price > trend.trend_high * (1 + thr):
            return "long"
        if price < trend.trend_low  * (1 - thr):
            return "short"
        return None

    def check_exit(self, trend: TrendAnalysis, price: float) -> Optional[str]:
        pos = self.position
        if not pos:
            return None
        # Stop loss kontrolü
        if pos.side == "long"  and price <= pos.stop_loss: return "STOP LOSS"
        if pos.side == "short" and price >= pos.stop_loss: return "STOP LOSS"
        # Trend tersine döndü mü?
        if pos.side == "long"  and trend.direction == "down" and price < trend.trend_low:
            return "TREND BOZULDU"
        if pos.side == "short" and trend.direction == "up"   and price > trend.trend_high:
            return "TREND BOZULDU"
        return None

    # ── Position sizing ───────────────────────────────────────────────────────

    def _calc_quantity(self, price: float) -> float:
        """Notional değeri base asset miktarına çevir, stepSize'a göre yuvarla."""
        step = self.executor._qty_step if self.executor else self._min_qty

        notional = self.config.trade_size_usdt * self.config.leverage
        MIN_NOTIONAL = 100.0
        if notional < MIN_NOTIONAL:
            notional = MIN_NOTIONAL
            logger.warning(f"Notional {self.config.trade_size_usdt * self.config.leverage} < {MIN_NOTIONAL} → {MIN_NOTIONAL} USDT")

        qty = notional / price
        # CEIL (yukarı yuvarla) → notional her zaman minimum karşılar
        qty_rounded = math.ceil(qty / step) * step
        prec = self.executor._qty_prec if self.executor else 3
        return round(qty_rounded, prec)

    # ── Order execution (live or simulated) ──────────────────────────────────

    async def open_position(self, side: str, price: float):
        cfg      = self.config
        sl_price = price * (1 - cfg.stop_loss_pct) if side == "long" else price * (1 + cfg.stop_loss_pct)
        quantity = self._calc_quantity(price)

        order_id      = 0
        stop_order_id = 0

        if self.executor and cfg.mode == "live":
            try:
                # 1. Kaldıraç + marjin modu
                await self.executor.set_leverage(cfg.symbol, cfg.leverage)
                await self.executor.set_margin_type(cfg.symbol, "ISOLATED")

                # 2. Market giriş emri
                order_side = "BUY" if side == "long" else "SELL"
                result     = await self.executor.market_order(
                    cfg.symbol, order_side, quantity
                )
                order_id   = result.get("orderId", 0)
                avg = float(result.get("avgPrice") or 0)
                if avg > 0:
                    price    = avg
                    sl_price = price * (1 - cfg.stop_loss_pct) if side == "long" else price * (1 + cfg.stop_loss_pct)

                sl_side   = "SELL" if side == "long" else "BUY"
                sl_result = await self.executor.stop_market_order(
                    cfg.symbol, sl_side, quantity, sl_price
                )
                stop_order_id = sl_result.get("orderId", 0)

            except Exception as e:
                logger.error(f"Emir açma hatası: {e}")
                self._log_trade("info", f"⚠ Emir hatası: {e}")
                return

        self.position = Position(
            side          = side,
            entry_price   = price,
            size_usdt     = cfg.trade_size_usdt,
            leverage      = cfg.leverage,
            stop_loss     = sl_price,
            open_time     = datetime.utcnow().isoformat(),
            symbol        = cfg.symbol,
            order_id      = order_id,
            stop_order_id = stop_order_id,
            quantity      = quantity,
        )

        mode_tag = "🔴 CANLI" if cfg.mode == "live" else "📊 SİM"
        msg = (f"{mode_tag} {side.upper()} açıldı @ {price:.4f} "
               f"| qty={quantity} | SL: {sl_price:.4f} "
               f"| {cfg.trade_size_usdt}$ × {cfg.leverage}x")
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
                self._log_trade("info", f"⚠ Kapatma hatası: {e}")

        pnl = pos.unrealized_pnl(price)
        pct = pnl / (pos.size_usdt * pos.leverage) * 100

        self.stats.total_trades    += 1
        self.stats.wins            += (1 if pnl > 0 else 0)
        self.stats.losses          += (1 if pnl <= 0 else 0)
        self.stats.total_pnl       += pnl
        self.stats.last_trade_time  = datetime.utcnow().isoformat()

        msg = f"KAPANDI ({reason}) @ {price:.4f} | PnL: {pnl:+.4f}$ ({pct:+.2f}%)"
        logger.info(msg)
        self._log_trade("close", msg, pnl=pnl)
        self.position = None

    # ── Log helper ────────────────────────────────────────────────────────────

    def _log_trade(self, type_: str, msg: str, pnl: float = 0.0):
        ts = int(time.time() * 1000)
        if self.trade_log:
            ts = max(ts, self.trade_log[-1]["ts"] + 1)
        self.trade_log.append({
            "type": type_,
            "msg":  msg,
            "pnl":  round(pnl, 4),
            "time": datetime.utcnow().strftime("%H:%M:%S"),
            "ts":   ts,
        })
        if len(self.trade_log) > 200:
            self.trade_log = self.trade_log[-200:]

    # ── Main candle handler ───────────────────────────────────────────────────

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

            if not self.position:
                signal = self.detect_breakout(trend, candle.close)
                if signal:
                    logger.info(f"🎯 Candle breakout: {signal} @ {candle.close:.2f} | high={trend.trend_high:.2f} low={trend.trend_low:.2f}")
                    await self.open_position(signal, candle.close)

    async def on_price_tick(self, price: float):
        """
        Gerçek zamanlı fiyat güncellemesi — mum kapanmadan breakout tespiti.
        Ticker stream'den her ~300ms çağrılır.
        """
        if not self.running or self._lock.locked():
            return
        self.current_price = price
        trend = self.current_trend
        if not trend:
            return

        # Stop loss gerçek zamanlı kontrol
        if self.position:
            if self.position.side == "long"  and price <= self.position.stop_loss:
                async with self._lock:
                    await self.close_position(price, "STOP LOSS (RT)")
            elif self.position.side == "short" and price >= self.position.stop_loss:
                async with self._lock:
                    await self.close_position(price, "STOP LOSS (RT)")
            return

        # Breakout gerçek zamanlı
        signal = self.detect_breakout(trend, price)
        if signal:
            async with self._lock:
                if not self.position:  # lock alındıktan sonra tekrar kontrol
                    logger.info(f"🎯 RT breakout: {signal} @ {price:.2f} | high={trend.trend_high:.2f} low={trend.trend_low:.2f}")
                    await self.open_position(signal, price)

    # ── State snapshot ────────────────────────────────────────────────────────

    def get_state(self) -> dict:
        wr = 0
        if self.stats.total_trades > 0:
            wr = round(self.stats.wins / self.stats.total_trades * 100, 1)

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
        }
