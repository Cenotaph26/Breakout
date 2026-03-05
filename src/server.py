"""
TrendBreak Bot — FastAPI Server
Railway Variables'dan config okur, başlayınca otomatik çalışır.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse

from src.bot import BotConfig, TrendBreakBot
from src.executor import BinanceExecutor
from src.feed import BinanceFeed, SimFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Global state ──────────────────────────────────────────────────────────────
bot:            TrendBreakBot | None = None
feed:           BinanceFeed | SimFeed | None = None
executor_inst:  BinanceExecutor | None = None
ws_clients:     set[WebSocket] = set()
broadcast_task: asyncio.Task | None = None


# ── Config from environment ───────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def load_config_from_env() -> dict:
    """Railway Variables'dan bot ayarlarını oku."""
    return {
        "api_key":         _env("BINANCE_API_KEY"),
        "api_secret":      _env("BINANCE_API_SECRET"),
        "demo":            _env("BINANCE_DEMO", "true").lower() != "false",
        "symbol":          _env("SYMBOL", "BTCUSDT"),
        "mode":            _env("MODE", "live"),     # 'live' | 'sim'
        "trend_period":    _env_int("TREND_PERIOD", 5),
        "break_threshold": _env_float("BREAK_THRESHOLD", 0.05),   # %
        "trade_size_usdt": _env_float("TRADE_SIZE", 15.0),
        "leverage":        _env_int("LEVERAGE", 3),
        "stop_loss_pct":   _env_float("STOP_LOSS_PCT", 0.5),      # %
        "tick_interval":   _env_float("TICK_INTERVAL", 2.0),      # sim only
    }


# ── Bot lifecycle ─────────────────────────────────────────────────────────────

async def start_bot_from_env():
    """Uygulama başlarken env var'lardan botu otomatik başlat."""
    global bot, feed, executor_inst

    cfg_raw = load_config_from_env()
    mode    = cfg_raw["mode"]

    if mode == "live" and (not cfg_raw["api_key"] or not cfg_raw["api_secret"]):
        logger.warning("⚠  BINANCE_API_KEY / BINANCE_API_SECRET eksik — simülasyon modunda başlatılıyor.")
        mode = "sim"

    cfg = BotConfig(
        symbol          = cfg_raw["symbol"].upper(),
        trend_period    = cfg_raw["trend_period"],
        break_threshold = cfg_raw["break_threshold"] / 100,
        trade_size_usdt = cfg_raw["trade_size_usdt"],
        leverage        = cfg_raw["leverage"],
        stop_loss_pct   = cfg_raw["stop_loss_pct"] / 100,
        mode            = mode,
    )

    # Executor oluştur (sadece live modda)
    exc = None
    if mode == "live":
        exc = BinanceExecutor(
            api_key    = cfg_raw["api_key"],
            api_secret = cfg_raw["api_secret"],
            demo       = cfg_raw["demo"],
        )
        # Exchange'den min lot size al
        try:
            min_qty = await exc.get_min_qty(cfg.symbol)
            logger.info(f"Min qty for {cfg.symbol}: {min_qty}")
        except Exception as e:
            logger.warning(f"Min qty alınamadı: {e} — varsayılan 0.001 kullanılıyor")
            min_qty = 0.001
    else:
        min_qty = 0.001

    bot          = TrendBreakBot(cfg, executor=exc)
    bot._min_qty = min_qty
    bot.running  = True
    executor_inst = exc

    if mode == "live":
        feed = BinanceFeed(bot)
    else:
        feed = SimFeed(bot, tick_interval=cfg_raw["tick_interval"])

    await feed.start()
    logger.info(
        f"🚀 Bot başlatıldı | {cfg.symbol} | mod={mode} | "
        f"kaldıraç={cfg.leverage}x | işlem={cfg.trade_size_usdt}$ | "
        f"demo={cfg_raw.get('demo', True)}"
    )


async def stop_bot_internal():
    global bot, feed, executor_inst
    if not bot or not bot.running:
        return
    bot.running = False
    if feed:
        await feed.stop()
        feed = None
    if bot.position:
        await bot.close_position(bot.current_price, "MANUEL DURDURMA")
    if executor_inst:
        await executor_inst.close()
        executor_inst = None
    logger.info("Bot durduruldu.")


# ── Broadcast ─────────────────────────────────────────────────────────────────

async def broadcast_loop():
    while True:
        await asyncio.sleep(1)
        if not bot or not ws_clients:
            continue
        state = json.dumps(bot.get_state())
        dead  = set()
        for ws in list(ws_clients):
            try:
                await ws.send_text(state)
            except Exception:
                dead.add(ws)
        ws_clients -= dead


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcast_task
    broadcast_task = asyncio.create_task(broadcast_loop())
    # Auto-start
    await start_bot_from_env()
    yield
    # Shutdown
    await stop_bot_internal()
    if broadcast_task:
        broadcast_task.cancel()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="TrendBreak Bot", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
async def health():
    return {"ok": True, "running": bool(bot and bot.running), "mode": bot.config.mode if bot else None}


@app.get("/api/state")
async def get_state():
    if not bot:
        return {"running": False}
    return bot.get_state()


# ── Manual controls (dashboard için) ─────────────────────────────────────────

@app.post("/api/stop")
async def api_stop():
    if not bot or not bot.running:
        raise HTTPException(400, "Bot zaten durmuyor.")
    await stop_bot_internal()
    return {"status": "stopped"}


@app.post("/api/restart")
async def api_restart():
    """Botu durdurup env var'larla yeniden başlat."""
    await stop_bot_internal()
    await asyncio.sleep(1)
    await start_bot_from_env()
    return {"status": "restarted"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    logger.info(f"WS bağlandı. Toplam: {len(ws_clients)}")
    try:
        if bot:
            await websocket.send_text(json.dumps(bot.get_state()))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)
