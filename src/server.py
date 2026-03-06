"""TrendBreak Bot — FastAPI Server"""

import asyncio, json, logging, os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from src.bot import BotConfig, TrendBreakBot, Candle
from src.executor import BinanceExecutor
from src.feed import BinanceFeed, SimFeed

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ── Globals ───────────────────────────────────────────────────
bot:            TrendBreakBot | None = None
feed:           BinanceFeed | SimFeed | None = None
executor_inst:  BinanceExecutor | None = None
ws_clients:     set[WebSocket] = set()
broadcast_task: asyncio.Task | None = None
startup_error:  str  = ""
binance_ok:     bool = False

# ── Env ───────────────────────────────────────────────────────
def _e(k, d=""): return os.environ.get(k, d).strip()
def _ef(k, d):
    try: return float(os.environ.get(k, d))
    except: return d
def _ei(k, d):
    try: return int(os.environ.get(k, d))
    except: return d

def load_env():
    return {
        "api_key":   _e("BINANCE_API_KEY"),
        "api_secret":_e("BINANCE_API_SECRET"),
        "demo":      _e("BINANCE_DEMO","true").lower()!="false",
        "symbol":    _e("SYMBOL","BTCUSDT"),
        "mode":      _e("MODE","live"),
        "trend_period":    _ei("TREND_PERIOD", 5),
        "break_threshold": _ef("BREAK_THRESHOLD", 0.05),
        "trade_size_usdt": _ef("TRADE_SIZE", 15.0),
        "leverage":        _ei("LEVERAGE", 3),
        "stop_loss_pct":   _ef("STOP_LOSS_PCT", 0.5),
        "tick_interval":   _ef("TICK_INTERVAL", 2.0),
    }

# ── Lifecycle ─────────────────────────────────────────────────
async def start_bot_from_env():
    global bot, feed, executor_inst, startup_error, binance_ok
    startup_error = ""; binance_ok = False
    raw  = load_env()
    mode = raw["mode"]
    logger.info(f"=== BOT BAŞLATILIYOR | mode={mode} | symbol={raw['symbol']} ===")

    if mode == "live" and (not raw["api_key"] or not raw["api_secret"]):
        startup_error = "BINANCE_API_KEY veya BINANCE_API_SECRET eksik!"
        logger.warning(f"⚠ {startup_error} → SIM moduna geçildi")
        mode = "sim"

    def mk_cfg(m):
        return BotConfig(
            symbol=raw["symbol"].upper(),
            trend_period=raw["trend_period"],
            break_threshold=raw["break_threshold"]/100,
            trade_size_usdt=raw["trade_size_usdt"],
            leverage=raw["leverage"],
            stop_loss_pct=raw["stop_loss_pct"]/100,
            mode=m,
        )

    cfg = mk_cfg(mode)
    exc = None; min_qty = 0.001; precision = 3

    if mode == "live":
        exc = BinanceExecutor(raw["api_key"], raw["api_secret"], demo=raw["demo"])
        try:
            await exc.test_connection()
            await exc.load_symbol_filters(cfg.symbol)   # tickSize + stepSize
            binance_ok = True
            logger.info(f"✅ Binance OK | qty_step={exc._qty_step} price_step={exc._price_step}")
        except Exception as e:
            startup_error = f"Binance hatası: {str(e)[:150]}"
            logger.error(f"❌ {startup_error}")
            await exc.close(); exc = None
            mode = "sim"; cfg = mk_cfg("sim")

    bot = TrendBreakBot(cfg, executor=exc)
    bot.running    = True
    executor_inst  = exc

    # Geçmiş mumları yükle
    if mode == "live" and exc:
        try:
            hist = await exc.get_recent_klines(cfg.symbol, limit=80)
            logger.info(f"Kline yanıtı: {len(hist)} mum")
            for k in hist:
                bot.candles.append(Candle(
                    open=k["open"], high=k["high"], low=k["low"],
                    close=k["close"], volume=k["volume"], timestamp=k["timestamp"],
                ))
            if hist:
                bot.current_price = hist[-1]["close"]
                bot.current_trend = bot.analyze_trend()
                logger.info(f"📊 {len(hist)} mum yüklendi | fiyat={bot.current_price:.2f} | trend={bot.current_trend.direction if bot.current_trend else 'yok'}")
        except Exception as e:
            logger.error(f"Kline yükleme hatası: {e}")

    feed = BinanceFeed(bot, demo=raw["demo"]) if mode=="live" else SimFeed(bot, tick_interval=raw["tick_interval"])
    await feed.start()
    logger.info(f"🚀 Bot aktif | mod={mode} | lev={cfg.leverage}x | size={cfg.trade_size_usdt}$")


async def stop_bot_internal():
    global bot, feed, executor_inst
    if not bot or not bot.running: return
    bot.running = False
    if feed: await feed.stop(); feed = None
    if bot.position: await bot.close_position(bot.current_price, "MANUEL DURDURMA")
    if executor_inst: await executor_inst.close(); executor_inst = None
    logger.info("Bot durduruldu.")


# ── Broadcast ─────────────────────────────────────────────────
def _state(full: bool = True):
    s = bot.get_state() if bot else {"running": False}
    s["startup_error"] = startup_error
    s["binance_ok"]    = binance_ok
    if not full:
        # Sadece anlık değişenler — candles ve trade_log çıkar
        s.pop("candles",    None)
        s.pop("trade_log",  None)
    return s

_tick = 0

async def broadcast_loop():
    global _tick
    while True:
        await asyncio.sleep(1)
        if not ws_clients: continue
        _tick += 1
        # Her 5 saniyede tam state (candles dahil), arası sadece fiyat/pozisyon
        full    = (_tick % 5 == 0)
        payload = json.dumps(_state(full=full))
        dead = set()
        for ws in list(ws_clients):
            try: await ws.send_text(payload)
            except: dead.add(ws)
        ws_clients -= dead

# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcast_task
    broadcast_task = asyncio.create_task(broadcast_loop())
    await start_bot_from_env()
    yield
    await stop_bot_internal()
    if broadcast_task: broadcast_task.cancel()

app = FastAPI(title="TrendBreak Bot", lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/index.html", encoding="utf-8") as f: return f.read()

@app.get("/api/health")
async def health():
    return {"ok": True, "running": bool(bot and bot.running),
            "mode": bot.config.mode if bot else None,
            "binance_ok": binance_ok, "error": startup_error or None,
            "candles": len(bot.candles) if bot else 0}

@app.get("/api/state")
async def state(): return _state(full=True)

@app.get("/api/balance")
async def balance():
    if not executor_inst: return {"balance": None, "error": "Live mod aktif değil"}
    try: return {"balance": round(await executor_inst.get_balance(), 2)}
    except Exception as e: return {"balance": None, "error": str(e)[:80]}

@app.post("/api/stop")
async def api_stop():
    if not bot or not bot.running: raise HTTPException(400, "Bot çalışmıyor.")
    await stop_bot_internal(); return {"status": "stopped"}

@app.post("/api/restart")
async def api_restart():
    await stop_bot_internal(); await asyncio.sleep(1)
    await start_bot_from_env(); return {"status": "restarted"}

# ── WebSocket ─────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    logger.info(f"WS bağlandı | toplam={len(ws_clients)}")

    async def _drain():
        """Gelen tüm mesajları oku (bağlantıyı canlı tutar)."""
        try:
            while True:
                await websocket.receive_text()
        except Exception:
            pass

    drain = asyncio.create_task(_drain())
    try:
        await websocket.send_text(json.dumps(_state(full=True)))
        while not drain.done():
            await asyncio.sleep(20)
            try:
                await websocket.send_text('{"ping":true}')
            except Exception:
                break
    except Exception:
        pass
    finally:
        drain.cancel()
        ws_clients.discard(websocket)
        logger.info(f"WS ayrıldı | toplam={len(ws_clients)}")
