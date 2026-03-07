"""TrendBreak Bot — FastAPI Server v3."""

import asyncio, json, logging, os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.responses import HTMLResponse
from src.bot import Config, Bot, Candle
from src.executor import Executor
from src.feed import BinanceFeed, SimFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
# Bot debug logs da görmek için:
logging.getLogger("bot").setLevel(logging.DEBUG)
logging.getLogger("feed").setLevel(logging.INFO)

log = logging.getLogger("server")

bot: Bot | None = None
feed = None
exc: Executor | None = None
ws_clients: set = set()
bc_task = None
error_msg = ""
bnc_ok = False


def env(k, d=""):
    return os.environ.get(k, d).strip()

def envf(k, d):
    try:
        return float(os.environ.get(k, d))
    except:
        return d

def envi(k, d):
    try:
        return int(os.environ.get(k, d))
    except:
        return d


async def start_bot():
    global bot, feed, exc, error_msg, bnc_ok
    error_msg = ""
    bnc_ok = False

    mode = env("MODE", "live")
    sym = env("SYMBOL", "BTCUSDT").upper()
    demo = env("BINANCE_DEMO", "true").lower() != "false"
    key = env("BINANCE_API_KEY")
    secret = env("BINANCE_API_SECRET")

    log.info(f"{'='*50}")
    log.info(f"BAŞLATILIYOR | mode={mode} sym={sym} demo={demo}")
    log.info(f"API key: {'var' if key else 'YOK'} | Secret: {'var' if secret else 'YOK'}")
    log.info(f"{'='*50}")

    if mode == "live" and (not key or not secret):
        error_msg = "API key/secret eksik → SIM moduna geçildi"
        log.warning(error_msg)
        mode = "sim"

    cfg = Config(
        symbol=sym,
        period=envi("TREND_PERIOD", 5),
        threshold=envf("BREAK_THRESHOLD", 0.05) / 100,
        size_usdt=envf("TRADE_SIZE", 100.0),
        leverage=envi("LEVERAGE", 5),
        sl_pct=envf("STOP_LOSS_PCT", 0.5) / 100,
        cooldown=envf("COOLDOWN", 15.0),
        mode=mode,
    )

    log.info(f"Config: period={cfg.period} thr={cfg.threshold} size={cfg.size_usdt} lev={cfg.leverage} sl={cfg.sl_pct} cd={cfg.cooldown}")

    _exc = None
    if mode == "live":
        _exc = Executor(key, secret, demo)
        try:
            await _exc.test()
            await _exc.load_filters(sym)
            bnc_ok = True
            log.info(f"Binance bağlantısı OK")
        except Exception as e:
            error_msg = f"Binance hatası: {str(e)[:120]}"
            log.error(error_msg)
            await _exc.close()
            _exc = None
            cfg.mode = "sim"
            mode = "sim"
            log.info("SIM moduna düşüldü")

    bot = Bot(cfg, _exc)
    bot.running = True
    exc = _exc

    # Geçmiş mumları yükle
    if _exc and mode == "live":
        try:
            hist = await _exc.klines(sym, 80)
            for k in hist:
                bot.candles.append(Candle(o=k["o"], h=k["h"], l=k["l"], c=k["c"], v=k["v"], t=k["t"]))
            if hist:
                bot.price = hist[-1]["c"]
                bot._update_trend()
                log.info(f"{len(hist)} mum yüklendi | fiyat={bot.price:.2f} | trend={bot.trend}")
            else:
                log.warning("Hiç mum yüklenemedi!")
        except Exception as e:
            log.error(f"Kline hatası: {e}")
    else:
        log.info("SIM modu — geçmiş mum yüklenmedi")

    tick = envf("TICK_INTERVAL", 2.0)
    feed = BinanceFeed(bot, demo) if mode == "live" else SimFeed(bot, tick)
    await feed.start()
    log.info(f"Bot AKTİF | {mode} | {cfg.leverage}x | {cfg.size_usdt}$")
    log.info(f"Mum sayısı: {len(bot.candles)} | Trend: {bot.trend}")


async def stop_bot():
    global bot, feed, exc
    if not bot or not bot.running:
        return
    log.info("Bot durduruluyor...")
    bot.running = False
    if feed:
        await feed.stop()
        feed = None
    if bot.pos:
        await bot._close(bot.price, "MANUEL DURDURMA")
    if exc:
        await exc.close()
        exc = None
    log.info("Bot durdu")


def get_state():
    s = bot.state() if bot else {"running": False}
    s["error"] = error_msg
    s["bnc_ok"] = bnc_ok
    return s


async def broadcast():
    while True:
        await asyncio.sleep(1)
        if not ws_clients:
            continue
        try:
            payload = json.dumps(get_state())
        except Exception as e:
            log.error(f"State serialize hatası: {e}")
            continue
        dead = set()
        for ws in list(ws_clients):
            try:
                await ws.send_text(payload)
            except:
                dead.add(ws)
        ws_clients -= dead


@asynccontextmanager
async def lifespan(app):
    global bc_task
    bc_task = asyncio.create_task(broadcast())
    await start_bot()
    yield
    await stop_bot()
    if bc_task:
        bc_task.cancel()


app = FastAPI(title="TrendBreak", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", encoding="utf-8") as f:
        return f.read()


@app.get("/api/health")
async def health():
    return {"ok": True, "running": bool(bot and bot.running), "bnc_ok": bnc_ok, "error": error_msg or None}


@app.get("/api/state")
async def api_state():
    return get_state()


@app.get("/api/balance")
async def api_balance():
    if not exc:
        return {"balance": None}
    try:
        return {"balance": round(await exc.get_balance(), 2)}
    except Exception as e:
        return {"balance": None, "error": str(e)[:80]}


@app.post("/api/stop")
async def api_stop():
    await stop_bot()
    return {"ok": True}


@app.post("/api/restart")
async def api_restart():
    await stop_bot()
    await asyncio.sleep(1)
    await start_bot()
    return {"ok": True}


@app.post("/api/config")
async def api_config(body: dict):
    mapping = {
        "symbol": "SYMBOL", "period": "TREND_PERIOD", "threshold": "BREAK_THRESHOLD",
        "size_usdt": "TRADE_SIZE", "leverage": "LEVERAGE", "sl_pct": "STOP_LOSS_PCT",
        "mode": "MODE", "demo": "BINANCE_DEMO", "cooldown": "COOLDOWN",
    }
    for k, e in mapping.items():
        if k in body:
            os.environ[e] = str(body[k]).lower()
    log.info(f"Config güncellendi: {body}")
    await stop_bot()
    await asyncio.sleep(0.5)
    await start_bot()
    return {"ok": True}


@app.websocket("/ws")
async def ws_ep(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    log.info(f"WS bağlandı | toplam={len(ws_clients)}")
    try:
        # İlk state gönder
        await websocket.send_text(json.dumps(get_state()))
        # Client'tan gelen mesajları dinle (keep-alive)
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await websocket.send_text('{"ping":true}')
                except:
                    break
    except:
        pass
    finally:
        ws_clients.discard(websocket)
        log.info(f"WS ayrıldı | toplam={len(ws_clients)}")
