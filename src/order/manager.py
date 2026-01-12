"""
Order manager for Predict market making.

Handles:
- Order placement based on strategy
- Order tracking and state management
- Order cancellation and updates
- Price comparison and rebalancing
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, List, Set
from dataclasses import dataclass, field

from ..config import get_settings, PricingMode
from ..api import PredictClient, PredictAPIError
from ..utils import (
    get_logger,
    PredictOrder,
    ExternalQuote,
    PredictMarket,
    Outcome,
    OrderSide,
    OrderStatus,
    round_price,
    calculate_target_price,
)

logger = get_logger(__name__)


@dataclass
class OrderTarget:
    """Target order parameters based on strategy."""
    market_id: str
    outcome: Outcome
    side: OrderSide
    price: Decimal
    size: Decimal

    def matches(self, order: PredictOrder, price_tolerance: Decimal = Decimal("0.001")) -> bool:
        """Check if an existing order matches this target within tolerance."""
        return (
            order.market_id == self.market_id and
            order.outcome == self.outcome and
            order.side == self.side and
            abs(order.price - self.price) <= price_tolerance and
            order.remaining_size >= self.size * Decimal("0.9")  # Allow 10% fill
        )


@dataclass
class MarketOrders:
    """Tracks orders for a single market."""
    market_id: str
    orders: Dict[str, PredictOrder] = field(default_factory=dict)
    targets: List[OrderTarget] = field(default_factory=list)
    last_update: Optional[datetime] = None

    def add_order(self, order: PredictOrder) -> None:
        """Add or update an order."""
        self.orders[order.order_id] = order
        self.last_update = datetime.utcnow()

    def remove_order(self, order_id: str) -> None:
        """Remove an order."""
        if order_id in self.orders:
            del self.orders[order_id]

    def get_open_orders(self) -> List[PredictOrder]:
        """Get all open orders."""
        return [
            o for o in self.orders.values()
            if o.status in (OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.PARTIALLY_FILLED)
        ]

    def get_orders_by_outcome(self, outcome: Outcome) -> List[PredictOrder]:
        """Get open orders for a specific outcome."""
        return [o for o in self.get_open_orders() if o.outcome == outcome]


class OrderManager:
    """
    Manages order placement and tracking for market making.

    Responsibilities:
    - Calculate target orders based on external quotes and strategy
    - Place, update, and cancel orders on Predict
    - Track order state and match orders to targets
    """

    def __init__(self, predict_client: PredictClient):
        """
        Initialize order manager.

        Args:
            predict_client: Predict API client
        """
        self._predict = predict_client

        # Order state per market
        self._markets: Dict[str, MarketOrders] = {}

        # Global tracking
        self._pending_cancels: Set[str] = set()

        # Strategy overrides per market
        self._strategy_overrides: Dict[str, dict] = {}

    def register_market(self, market_id: str) -> None:
        """Register a market for order management."""
        if market_id not in self._markets:
            self._markets[market_id] = MarketOrders(market_id=market_id)

    def unregister_market(self, market_id: str) -> None:
        """Unregister a market."""
        if market_id in self._markets:
            del self._markets[market_id]

    def set_strategy_override(self, market_id: str, **kwargs) -> None:
        """Set strategy parameter overrides for a market."""
        self._strategy_overrides[market_id] = kwargs

    def get_strategy_params(self, market_id: str) -> dict:
        """Get effective strategy parameters for a market."""
        settings = get_settings()
        base = {
            "pricing_mode": settings.strategy.pricing_mode,
            "offset": settings.strategy.offset,
            "order_size": settings.strategy.order_size,
            "auto_pricing_enabled": settings.strategy.auto_pricing_enabled,
        }
        # Apply overrides
        if market_id in self._strategy_overrides:
            base.update(self._strategy_overrides[market_id])
        return base

    def calculate_targets(
        self,
        market: PredictMarket,
        quote: ExternalQuote
    ) -> List[OrderTarget]:
        """
        Calculate target orders based on external quote and strategy.

        Args:
            market: Predict market info
            quote: External market quote

        Returns:
            List of OrderTarget to achieve
        """
        params = self.get_strategy_params(market.market_id)

        if not params.get("auto_pricing_enabled", True):
            return []

        targets = []
        pricing_mode = params["pricing_mode"]
        offset = Decimal(str(params["offset"]))
        size = Decimal(str(params["order_size"]))
        precision = market.decimal_precision

        if pricing_mode == PricingMode.SELL_ONLY:
            # Only place sell orders (asks)
            # Reference: external ask price

            # YES sell order
            if quote.yes_ask is not None:
                yes_price = calculate_target_price(
                    quote.yes_ask, offset, is_sell=True, decimal_precision=precision
                )
                targets.append(OrderTarget(
                    market_id=market.market_id,
                    outcome=Outcome.YES,
                    side=OrderSide.SELL,
                    price=yes_price,
                    size=size
                ))

            # NO sell order
            if quote.no_ask is not None:
                no_price = calculate_target_price(
                    quote.no_ask, offset, is_sell=True, decimal_precision=precision
                )
                targets.append(OrderTarget(
                    market_id=market.market_id,
                    outcome=Outcome.NO,
                    side=OrderSide.SELL,
                    price=no_price,
                    size=size
                ))

        elif pricing_mode == PricingMode.TWO_SIDED:
            # Place both buy and sell orders

            # YES side
            if quote.yes_bid is not None:
                yes_bid_price = calculate_target_price(
                    quote.yes_bid, offset, is_sell=False, decimal_precision=precision
                )
                targets.append(OrderTarget(
                    market_id=market.market_id,
                    outcome=Outcome.YES,
                    side=OrderSide.BUY,
                    price=yes_bid_price,
                    size=size
                ))

            if quote.yes_ask is not None:
                yes_ask_price = calculate_target_price(
                    quote.yes_ask, offset, is_sell=True, decimal_precision=precision
                )
                targets.append(OrderTarget(
                    market_id=market.market_id,
                    outcome=Outcome.YES,
                    side=OrderSide.SELL,
                    price=yes_ask_price,
                    size=size
                ))

            # NO side
            if quote.no_bid is not None:
                no_bid_price = calculate_target_price(
                    quote.no_bid, offset, is_sell=False, decimal_precision=precision
                )
                targets.append(OrderTarget(
                    market_id=market.market_id,
                    outcome=Outcome.NO,
                    side=OrderSide.BUY,
                    price=no_bid_price,
                    size=size
                ))

            if quote.no_ask is not None:
                no_ask_price = calculate_target_price(
                    quote.no_ask, offset, is_sell=True, decimal_precision=precision
                )
                targets.append(OrderTarget(
                    market_id=market.market_id,
                    outcome=Outcome.NO,
                    side=OrderSide.SELL,
                    price=no_ask_price,
                    size=size
                ))

        return targets

    async def sync_orders(self, market_id: str) -> None:
        """
        Sync local order state with Predict API.

        Args:
            market_id: Market ID to sync
        """
        if market_id not in self._markets:
            return

        market_orders = self._markets[market_id]

        try:
            orders = await self._predict.get_open_orders(market_id=market_id)

            # Update local state
            market_orders.orders.clear()
            for order in orders:
                market_orders.add_order(order)

            market_orders.last_update = datetime.utcnow()

        except PredictAPIError as e:
            logger.error(f"Failed to sync orders: {e}", market_id=market_id)

    async def rebalance(
        self,
        market: PredictMarket,
        quote: ExternalQuote,
        price_tolerance: Decimal = Decimal("0.005")
    ) -> Dict[str, any]:
        """
        Rebalance orders for a market based on new quote.

        This will:
        1. Calculate target orders
        2. Cancel orders that don't match targets
        3. Place new orders to meet targets

        Args:
            market: Predict market
            quote: Current external quote
            price_tolerance: Price tolerance for matching

        Returns:
            Dict with rebalance results
        """
        market_id = market.market_id
        self.register_market(market_id)

        result = {
            "market_id": market_id,
            "cancelled": 0,
            "placed": 0,
            "kept": 0,
            "errors": []
        }

        # Calculate targets
        targets = self.calculate_targets(market, quote)
        self._markets[market_id].targets = targets

        if not targets:
            logger.debug(f"No targets for {market_id}")
            return result

        # Sync current orders
        await self.sync_orders(market_id)

        market_orders = self._markets[market_id]
        open_orders = market_orders.get_open_orders()

        # Match existing orders to targets
        matched_orders: Set[str] = set()
        matched_targets: Set[int] = set()

        for i, target in enumerate(targets):
            for order in open_orders:
                if order.order_id not in matched_orders:
                    if target.matches(order, price_tolerance):
                        matched_orders.add(order.order_id)
                        matched_targets.add(i)
                        result["kept"] += 1
                        break

        # Cancel unmatched orders
        for order in open_orders:
            if order.order_id not in matched_orders:
                try:
                    await self._predict.cancel_order(order.order_id)
                    market_orders.remove_order(order.order_id)
                    result["cancelled"] += 1
                except PredictAPIError as e:
                    result["errors"].append(f"Cancel {order.order_id}: {e}")

        # Place orders for unmatched targets
        for i, target in enumerate(targets):
            if i not in matched_targets:
                try:
                    order = await self._predict.create_order(
                        market_id=target.market_id,
                        outcome=target.outcome,
                        side=target.side,
                        price=target.price,
                        size=target.size
                    )
                    market_orders.add_order(order)
                    result["placed"] += 1
                except PredictAPIError as e:
                    result["errors"].append(f"Place {target.outcome.value} {target.side.value}: {e}")

        logger.info(
            f"Rebalance complete",
            market_id=market_id,
            cancelled=result["cancelled"],
            placed=result["placed"],
            kept=result["kept"]
        )

        return result

    async def cancel_all_orders(
        self,
        market_id: Optional[str] = None,
        use_fast_removal: bool = False
    ) -> int:
        """
        Cancel all open orders.

        Args:
            market_id: Optional market to cancel (all if None)
            use_fast_removal: Use fast removal (risky)

        Returns:
            Number of orders cancelled
        """
        total = 0

        if market_id:
            # Cancel for specific market
            if market_id in self._markets:
                await self.sync_orders(market_id)
                for order in self._markets[market_id].get_open_orders():
                    try:
                        await self._predict.cancel_order(order.order_id, use_fast_removal)
                        self._markets[market_id].remove_order(order.order_id)
                        total += 1
                    except PredictAPIError as e:
                        logger.error(f"Failed to cancel order {order.order_id}: {e}")
        else:
            # Cancel all
            for mid in list(self._markets.keys()):
                total += await self.cancel_all_orders(mid, use_fast_removal)

        return total

    def get_market_orders(self, market_id: str) -> Optional[MarketOrders]:
        """Get order state for a market."""
        return self._markets.get(market_id)

    def get_all_open_orders(self) -> List[PredictOrder]:
        """Get all open orders across all markets."""
        orders = []
        for market_orders in self._markets.values():
            orders.extend(market_orders.get_open_orders())
        return orders

    @property
    def total_open_orders(self) -> int:
        """Total number of open orders."""
        return len(self.get_all_open_orders())
