"""TrendBreak v4 Server — Multi-coin."""

import asyncio, json, logging, os
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from src.bot import Config, Bot, SymbolState
from src.executor import Executor
from src.feed import MultiFeed, SimFeed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
logging.getLogger("bot").setLevel(logging.INFO)
log = logging.getLogger("server")

bot: Bot | None = None; feed = None; exc: Executor | None = None
ws_clients: set = set(); bc_task = None; error_msg = ""; bnc_ok = False

def env(k,d=""): return os.environ.get(k,d).strip()
def envf(k,d):
    try: return float(os.environ.get(k,d))
    except: return d
def envi(k,d):
    try: return int(os.environ.get(k,d))
    except: return d


async def start_bot():
    global bot, feed, exc, error_msg, bnc_ok
    error_msg = ""; bnc_ok = False
    mode = env("MODE","live"); demo = env("BINANCE_DEMO","true").lower()!="false"
    key = env("BINANCE_API_KEY"); secret = env("BINANCE_API_SECRET")

    log.info(f"{'='*50}")
    log.info(f"BAŞLATILIYOR | mode={mode} demo={demo}")

    if mode == "live" and (not key or not secret):
        error_msg = "API key/secret eksik → SIM"; log.warning(error_msg); mode = "sim"

    cfg = Config(
        period=envi("TREND_PERIOD",5), threshold=envf("BREAK_THRESHOLD",0.05)/100,
        size_usdt=envf("TRADE_SIZE",20.0), leverage=envi("LEVERAGE",5),
        sl_pct=envf("STOP_LOSS_PCT",0.5)/100, cooldown=envf("COOLDOWN",30.0),
        max_positions=envi("MAX_POSITIONS",5), mode=mode,
        top_n=envi("TOP_N",30), min_volume_usdt=envf("MIN_VOLUME",5000000),
    )

    _exc = None
    if mode == "live":
        _exc = Executor(key, secret, demo)
        try:
            await _exc.test()
            await _exc.load_all_filters()
            bnc_ok = True
        except Exception as e:
            error_msg = f"Binance: {str(e)[:120]}"; log.error(error_msg)
            await _exc.close(); _exc = None; cfg.mode = "sim"; mode = "sim"

    bot = Bot(cfg, _exc); bot.running = True; exc = _exc

    if _exc and mode == "live":
        # Top coinleri bul
        symbols = await _exc.get_top_symbols(cfg.top_n, cfg.min_volume_usdt)
        bot.active_symbols = symbols
        bot._filters = _exc._filters

        # Her coin için sembol state oluştur + geçmiş mumları yükle
        log.info(f"{len(symbols)} coin için mumlar yükleniyor...")
        for sym in symbols:
            try:
                hist = await _exc.klines(sym, 80)
                ss = SymbolState(symbol=sym, candles=hist, price=hist[-1]["c"] if hist else 0)
                bot.symbols[sym] = ss
                bot._analyze(sym)
            except Exception as e:
                log.warning(f"{sym} kline hatası: {e}")
            await asyncio.sleep(0.05)  # rate limit

        log.info(f"{len(bot.symbols)} coin yüklendi")
        feed = MultiFeed(bot, symbols, demo)
    else:
        feed = SimFeed(bot, envf("TICK_INTERVAL", 2.0))

    await feed.start()
    log.info(f"Bot AKTİF | {mode} | {len(bot.active_symbols)} coin | max {cfg.max_positions} pozisyon")


async def stop_bot():
    global bot, feed, exc
    if not bot or not bot.running: return
    bot.running = False
    if feed: await feed.stop(); feed = None
    for sym in list(bot.positions.keys()):
        await bot._close(sym, bot.symbols.get(sym, SymbolState(sym, [])).price, "DURDURMA")
    if exc: await exc.close(); exc = None
    log.info("Bot durdu")


def get_state():
    s = bot.state() if bot else {"running": False}
    s["error"] = error_msg; s["bnc_ok"] = bnc_ok
    return s


async def broadcast():
    while True:
        await asyncio.sleep(1)
        if not ws_clients: continue
        try: payload = json.dumps(get_state())
        except: continue
        dead = set()
        for ws in list(ws_clients):
            try: await ws.send_text(payload)
            except: dead.add(ws)
        ws_clients -= dead


@asynccontextmanager
async def lifespan(app):
    global bc_task
    bc_task = asyncio.create_task(broadcast())
    await start_bot()
    yield
    await stop_bot()
    if bc_task: bc_task.cancel()

app = FastAPI(title="TrendBreak", lifespan=lifespan)

@app.get("/", response_class=HTMLResponse)
async def index():
    with open("templates/index.html", encoding="utf-8") as f: return f.read()

@app.get("/api/state")
async def api_state(): return get_state()

@app.get("/api/health")
async def api_health():
    return {"ok": True, "running": bool(bot and bot.running), "bnc_ok": bnc_ok}

@app.post("/api/stop")
async def api_stop(): await stop_bot(); return {"ok":True}

@app.post("/api/restart")
async def api_restart(): await stop_bot(); await asyncio.sleep(1); await start_bot(); return {"ok":True}

@app.post("/api/chart")
async def api_chart(body: dict):
    """Grafik coin'ini değiştir."""
    sym = body.get("symbol","").upper()
    if bot and sym in bot.symbols:
        # chart_sym'ı active_symbols'ın başına taşı
        if sym in bot.active_symbols:
            bot.active_symbols.remove(sym)
        bot.active_symbols.insert(0, sym)
    return {"ok": True, "symbol": sym}

@app.post("/api/config")
async def api_config(body: dict):
    m = {"size_usdt":"TRADE_SIZE","leverage":"LEVERAGE","period":"TREND_PERIOD",
         "threshold":"BREAK_THRESHOLD","sl_pct":"STOP_LOSS_PCT","mode":"MODE",
         "demo":"BINANCE_DEMO","cooldown":"COOLDOWN","max_positions":"MAX_POSITIONS",
         "top_n":"TOP_N"}
    for k,e in m.items():
        if k in body: os.environ[e] = str(body[k]).lower()
    await stop_bot(); await asyncio.sleep(0.5); await start_bot()
    return {"ok":True}

@app.websocket("/ws")
async def ws_ep(ws: WebSocket):
    await ws.accept(); ws_clients.add(ws)
    try:
        await ws.send_text(json.dumps(get_state()))
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                # Client coin seçimi gönderebilir
                try:
                    d = json.loads(msg)
                    if d.get("select_sym") and bot:
                        bot.selected_sym = d["select_sym"].upper()
                        # Hemen yeni state gönder
                        await ws.send_text(json.dumps(get_state()))
                except:
                    pass
            except asyncio.TimeoutError:
                try: await ws.send_text('{"ping":true}')
                except: break
    except: pass
    finally: ws_clients.discard(ws)
