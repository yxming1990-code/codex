import asyncio
from typing import Any, AsyncGenerator, Dict, Optional

from backend.app.services.polymarket_client import OrderBookUpdate, PolymarketClient


class TradingBot:
    def __init__(self, client: PolymarketClient) -> None:
        self.client = client
        self.trading = False
        self.active_market: Optional[str] = None
        self.strategy_name = "demo"
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._orderbook_queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None

    async def start_trading(self, market_id: Optional[str], strategy: str) -> None:
        self.active_market = market_id or "demo-market"
        self.strategy_name = strategy
        if not self.client.connected:
            await self.client.connect()
        if self._task:
            return
        self.trading = True
        self._orderbook_queue = await self.client.subscribe_orderbook(self.active_market)
        self._task = asyncio.create_task(self._run())
        await self._event_queue.put({"type": "status", "message": "Trading started"})

    async def stop_trading(self) -> None:
        self.trading = False
        if self._orderbook_queue:
            await self.client.unsubscribe_orderbook(self._orderbook_queue)
            self._orderbook_queue = None
        if self._task:
            self._task.cancel()
            self._task = None
        await self._event_queue.put({"type": "status", "message": "Trading stopped"})

    async def _run(self) -> None:
        while self.trading and self._orderbook_queue:
            update: OrderBookUpdate = await self._orderbook_queue.get()
            await self._event_queue.put(
                {
                    "type": "orderbook",
                    "market_id": update.market_id,
                    "bids": update.bids,
                    "asks": update.asks,
                }
            )
            # Demo trading logic placeholder.
            top_bid = update.bids[0]["price"]
            top_ask = update.asks[0]["price"]
            mid = round((top_bid + top_ask) / 2, 4)
            order = await self.client.place_order(
                market_id=update.market_id,
                side="buy",
                price=mid,
                size=1.0,
            )
            await self._event_queue.put({"type": "trade", "order": order})

    async def stream_events(self) -> AsyncGenerator[Dict[str, Any], None]:
        while True:
            event = await self._event_queue.get()
            yield event
