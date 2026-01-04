from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.app.services.bot import TradingBot
from backend.app.services.polymarket_client import PolymarketClient

app = FastAPI(title="Polymarket Auto Trader")

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

polymarket_client = PolymarketClient()
bot = TradingBot(polymarket_client)


@app.get("/")
async def index() -> HTMLResponse:
    with open("frontend/static/index.html", "r", encoding="utf-8") as handle:
        return HTMLResponse(handle.read())


@app.get("/api/status")
async def status() -> dict:
    return {
        "connected": polymarket_client.connected,
        "trading": bot.trading,
        "active_market": bot.active_market,
        "strategy": bot.strategy_name,
    }


@app.post("/api/connect")
async def connect() -> dict:
    await polymarket_client.connect()
    return {"status": "connected"}


@app.post("/api/disconnect")
async def disconnect() -> dict:
    await polymarket_client.disconnect()
    return {"status": "disconnected"}


@app.post("/api/start")
async def start_trading(payload: dict) -> dict:
    market_id = payload.get("market_id")
    strategy = payload.get("strategy", "demo")
    await bot.start_trading(market_id=market_id, strategy=strategy)
    return {"status": "started"}


@app.post("/api/stop")
async def stop_trading() -> dict:
    await bot.stop_trading()
    return {"status": "stopped"}


@app.websocket("/ws/events")
async def events_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    async for event in bot.stream_events():
        await websocket.send_json(event)
