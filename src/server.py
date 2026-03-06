"""
TrendBreak Bot — FastAPI Server
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

# ── Global ─────────────────────────────────────────────────────
bot:            TrendBreakBot | None = None
feed:           BinanceFeed | SimFeed | None = None
executor_inst:  BinanceExecutor | None = None
ws_clients:     set[WebSocket] = set()
broadcast_task: asyncio.Task | None = None

# Startup'ta oluşan hata mesajını dashboard'a iletmek için
startup_error:  str = ""
binance_ok:     bool = False


# ── Env helpers ────────────────────────────────────────────────

def _env(key, default=""):
    return os.environ.get(key, default).strip()

def _env_float(key, default):
    try:    return float(os.environ.get(key, default))
    except: return default

def _env_int(key, default):
    try:    return int(os.environ.get(key, default))
    except: return default

def load_env():
    return {
        "api_key":         _env("BINANCE_API_KEY"),
        "api_secret":      _env("BINANCE_API_SECRET"),
        "demo":            _env("BINANCE_DEMO", "true").lower() != "false",
        "symbol":          _env("SYMBOL", "BTCUSDT"),
        "mode":            _env("MODE", "live"),
        "trend_period":    _env_int("TREND_PERIOD", 5),
        "break_threshold": _env_float("BREAK_THRESHOLD", 0.05),
        "trade_size_usdt": _env_float("TRADE_SIZE", 15.0),
        "leverage":        _env_int("LEVERAGE", 3),
        "stop_loss_pct":   _env_float("STOP_LOSS_PCT", 0.5),
        "tick_interval":   _env_float("TICK_INTERVAL", 2.0),
    }


# ── Bot lifecycle ──────────────────────────────────────────────

async def start_bot_from_env():
    global bot, feed, executor_inst, startup_error, binance_ok

    startup_error = ""
    binance_ok    = False
    raw  = load_env()
    mode = raw["mode"]

    # API key kontrol
    if mode == "live" and (not raw["api_key"] or not raw["api_secret"]):
        startup_error = "BINANCE_API_KEY veya BINANCE_API_SECRET Railway Variables'da eksik!"
        logger.warning(f"⚠ {startup_error} → Simülasyon moduna geçildi.")
        mode = "sim"

    def make_cfg(m):
        return BotConfig(
            symbol          = raw["symbol"].upper(),
            trend_period    = raw["trend_period"],
            break_threshold = raw["break_threshold"] / 100,
            trade_size_usdt = raw["trade_size_usdt"],
            leverage        = raw["leverage"],
            stop_loss_pct   = raw["stop_loss_pct"] / 100,
            mode            = m,
        )

    cfg = make_cfg(mode)

    exc       = None
    min_qty   = 0.001
    precision = 3

    if mode == "live":
        exc = BinanceExecutor(
            api_key    = raw["api_key"],
            api_secret = raw["api_secret"],
            demo       = raw["demo"],
        )
        try:
            await exc.sync_time()
            await exc.test_connection()
            min_qty   = await exc.get_min_qty(cfg.symbol)
            precision = await exc.get_qty_precision(cfg.symbol)
            binance_ok = True
            logger.info(f"✅ Binance OK | {cfg.symbol} min_qty={min_qty} precision={precision}")
        except Exception as e:
            err = str(e)
            # Sadece ilk 120 karakteri al — URL/body kalabalık log yaratır
            startup_error = f"Binance bağlantı hatası: {err[:120]}"
            logger.error(f"❌ {startup_error}")
            await exc.close()
            exc  = None
            mode = "sim"
            cfg  = make_cfg("sim")

    bot            = TrendBreakBot(cfg, executor=exc)
    bot._min_qty   = min_qty
    bot._precision = precision
    bot.running    = True
    executor_inst  = exc

    feed = BinanceFeed(bot) if mode == "live" else SimFeed(bot, tick_interval=raw["tick_interval"])
    await feed.start()

    logger.info(
        f"🚀 Bot başlatıldı | {cfg.symbol} | mod={mode} | "
        f"lev={cfg.leverage}x | size={cfg.trade_size_usdt}$ | "
        f"demo={raw['demo']} | binance_ok={binance_ok}"
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


# ── Broadcast ──────────────────────────────────────────────────

def _make_state():
    state = bot.get_state() if bot else {"running": False}
    state["startup_error"] = startup_error
    state["binance_ok"]    = binance_ok
    return state

async def broadcast_loop():
    while True:
        await asyncio.sleep(1)
        if not ws_clients:
            continue
        payload = json.dumps(_make_state())
        dead = set()
        for ws in list(ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        ws_clients -= dead


# ── Lifespan ───────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcast_task
    broadcast_task = asyncio.create_task(broadcast_loop())
    await start_bot_from_env()
    yield
    await stop_bot_internal()
    if broadcast_task:
        broadcast_task.cancel()


# ── App ────────────────────────────────────────────────────────

app = FastAPI(title="TrendBreak Bot", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()

@app.get("/api/health")
async def health():
    return {
        "ok":          True,
        "running":     bool(bot and bot.running),
        "mode":        bot.config.mode if bot else None,
        "binance_ok":  binance_ok,
        "error":       startup_error or None,
    }

@app.get("/api/state")
async def get_state_api():
    return _make_state()

@app.get("/api/balance")
async def get_balance():
    if not executor_inst:
        return {"balance": None, "error": "Live mod aktif değil"}
    try:
        bal = await executor_inst.get_balance()
        return {"balance": round(bal, 2)}
    except Exception as e:
        return {"balance": None, "error": str(e)[:80]}

@app.post("/api/stop")
async def api_stop():
    if not bot or not bot.running:
        raise HTTPException(400, "Bot zaten durmuyor.")
    await stop_bot_internal()
    return {"status": "stopped"}

@app.post("/api/restart")
async def api_restart():
    await stop_bot_internal()
    await asyncio.sleep(1)
    await start_bot_from_env()
    return {"status": "restarted"}


# ── WebSocket ──────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    logger.info(f"WS bağlandı | toplam={len(ws_clients)}")
    try:
        # İlk state'i hemen gönder
        await websocket.send_text(json.dumps(_make_state()))

        # ── Keep-alive: her 20s ping, mesaj gelirse yoksay ──────────
        # receive_text() blocking olduğundan asyncio.wait kullanıyoruz
        while True:
            try:
                # 20 saniye bekle; herhangi bir mesaj gelse de devam et
                await asyncio.wait_for(websocket.receive_text(), timeout=20)
            except asyncio.TimeoutError:
                # Timeout → ping gönder (tarayıcı/proxy bağlantıyı kesmemesi için)
                await websocket.send_text(json.dumps({"ping": True}))
            except WebSocketDisconnect:
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS beklenmeyen hata: {e}")
    finally:
        ws_clients.discard(websocket)
        logger.info(f"WS ayrıldı | toplam={len(ws_clients)}")
