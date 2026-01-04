import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OrderBookUpdate:
    market_id: str
    bids: List[Dict[str, Any]] = field(default_factory=list)
    asks: List[Dict[str, Any]] = field(default_factory=list)


class PolymarketClient:
    """
    Placeholder client for Polymarket websocket/orderbook integrations.
    Replace the internal TODOs with real websocket subscriptions from:
    https://github.com/discountry/polymarket-orderbook-watcher
    https://github.com/discountry/polymarket-websocket-client
    """

    def __init__(self) -> None:
        self.connected = False
        self._listeners: List[asyncio.Queue] = []
        self._task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        if self.connected:
            return
        self.connected = True
        self._task = asyncio.create_task(self._mock_stream())

    async def disconnect(self) -> None:
        self.connected = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _mock_stream(self) -> None:
        """Simulate a market data stream for the UI."""
        counter = 0
        while self.connected:
            await asyncio.sleep(1)
            update = OrderBookUpdate(
                market_id="demo-market",
                bids=[{"price": 0.45 + counter * 0.001, "size": 100 + counter}],
                asks=[{"price": 0.55 + counter * 0.001, "size": 90 + counter}],
            )
            counter += 1
            for listener in list(self._listeners):
                await listener.put(update)

    async def subscribe_orderbook(self, market_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners.append(queue)
        return queue

    async def unsubscribe_orderbook(self, queue: asyncio.Queue) -> None:
        if queue in self._listeners:
            self._listeners.remove(queue)

    async def place_order(self, market_id: str, side: str, price: float, size: float) -> Dict[str, Any]:
        """Placeholder trading call."""
        await asyncio.sleep(0.1)
        return {
            "market_id": market_id,
            "side": side,
            "price": price,
            "size": size,
            "status": "simulated",
        }
