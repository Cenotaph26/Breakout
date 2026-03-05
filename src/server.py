"""
TrendBreak Bot — FastAPI Server
WebSocket ile gerçek zamanlı güncelleme + REST API
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.bot import BotConfig, TrendBreakBot
from src.feed import BinanceFeed, SimFeed

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# ── Global state ─────────────────────────────────────────────────────────────
bot: TrendBreakBot | None = None
feed: BinanceFeed | SimFeed | None = None
ws_clients: set[WebSocket] = set()
broadcast_task: asyncio.Task | None = None


# ── Broadcast loop ────────────────────────────────────────────────────────────
async def broadcast_loop():
    """Push bot state to all connected WebSocket clients every second."""
    while True:
        await asyncio.sleep(1)
        if not bot or not ws_clients:
            continue
        state = json.dumps(bot.get_state())
        dead = set()
        for ws in ws_clients:
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
    yield
    if broadcast_task:
        broadcast_task.cancel()
    if feed:
        await feed.stop()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="TrendBreak Bot", lifespan=lifespan)

# Static files
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ── Dashboard HTML ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


# ── REST API ──────────────────────────────────────────────────────────────────
class StartRequest(BaseModel):
    symbol: str = "BTCUSDT"
    trend_period: int = 5
    break_threshold: float = 0.05   # percent → converted internally
    trade_size_usdt: float = 100.0
    leverage: int = 5
    stop_loss_pct: float = 0.5      # percent → converted internally
    mode: str = "sim"               # 'sim' | 'live'
    tick_interval: float = 2.0      # sim only: seconds per candle


@app.post("/api/start")
async def start_bot(req: StartRequest):
    global bot, feed

    if bot and bot.running:
        raise HTTPException(400, "Bot zaten çalışıyor.")

    cfg = BotConfig(
        symbol=req.symbol.upper(),
        trend_period=req.trend_period,
        break_threshold=req.break_threshold / 100,
        trade_size_usdt=req.trade_size_usdt,
        leverage=req.leverage,
        stop_loss_pct=req.stop_loss_pct / 100,
        mode=req.mode,
    )

    bot = TrendBreakBot(cfg)
    bot.running = True

    if req.mode == "live":
        feed = BinanceFeed(bot)
    else:
        feed = SimFeed(bot, tick_interval=req.tick_interval)

    await feed.start()
    logger.info(f"Bot başlatıldı — {cfg.symbol} [{req.mode}]")
    return {"status": "started", "config": cfg.__dict__}


@app.post("/api/stop")
async def stop_bot():
    global bot, feed

    if not bot or not bot.running:
        raise HTTPException(400, "Bot çalışmıyor.")

    bot.running = False
    if feed:
        await feed.stop()
        feed = None

    # Açık pozisyonu kapat
    if bot.position:
        await bot.close_position(bot.current_price, "MANUEL DURDURMA")

    logger.info("Bot durduruldu.")
    return {"status": "stopped", "stats": bot.stats.__dict__}


@app.get("/api/state")
async def get_state():
    if not bot:
        return {"running": False}
    return bot.get_state()


@app.get("/api/health")
async def health():
    return {"ok": True, "running": bool(bot and bot.running)}


# ── WebSocket ─────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    logger.info(f"WS client bağlandı. Toplam: {len(ws_clients)}")
    try:
        # Send initial state immediately
        if bot:
            await websocket.send_text(json.dumps(bot.get_state()))
        while True:
            await websocket.receive_text()  # keep-alive
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(websocket)
        logger.info(f"WS client ayrıldı. Toplam: {len(ws_clients)}")
