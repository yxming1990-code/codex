"""
Predict API client for market data, orders, and positions.
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any, AsyncIterator

import httpx

from ..config import get_settings
from ..utils import (
    get_logger,
    get_predict_limiter,
    retry_with_backoff,
    PredictMarket,
    PredictOrder,
    Position,
    MatchEvent,
    OrderBook,
    PriceLevel,
    Outcome,
    OrderSide,
    OrderStatus,
    safe_decimal,
    complement_price,
)

logger = get_logger(__name__)


class PredictAPIError(Exception):
    """Predict API error."""

    def __init__(self, message: str, status_code: int = 0, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class PredictClient:
    """
    Async client for Predict.fun API.

    Handles:
    - Market discovery and metadata
    - Order book fetching
    - Order placement and cancellation
    - Position management
    - Match event retrieval
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None
    ):
        """
        Initialize Predict client.

        Args:
            api_key: Optional API key for authenticated requests
            base_url: Optional base URL override
        """
        settings = get_settings()
        self._api_key = api_key or (
            settings.predict_api_key.get_secret_value()
            if settings.predict_api_key else None
        )
        self._base_url = base_url or settings.predict_base_url
        self._timeout = settings.predict_api.timeout
        self._rate_limiter = get_predict_limiter()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._get_headers()
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Make a rate-limited API request.

        Args:
            method: HTTP method
            endpoint: API endpoint
            params: Query parameters
            json: JSON body
            **kwargs: Additional httpx kwargs

        Returns:
            Response JSON

        Raises:
            PredictAPIError: On API error
        """
        await self._rate_limiter.acquire()

        if not self._client:
            raise PredictAPIError("Client not initialized. Use async context manager.")

        try:
            response = await self._client.request(
                method,
                endpoint,
                params=params,
                json=json,
                **kwargs
            )

            if response.status_code == 429:
                # Rate limited - wait and retry
                retry_after = int(response.headers.get("Retry-After", "5"))
                logger.warning(f"Rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                raise PredictAPIError("Rate limited", 429, None)

            if response.status_code >= 400:
                error_body = response.text
                try:
                    error_body = response.json()
                except:
                    pass
                raise PredictAPIError(
                    f"API error: {response.status_code}",
                    response.status_code,
                    error_body
                )

            return response.json()

        except httpx.RequestError as e:
            raise PredictAPIError(f"Request failed: {e}")

    # ========== Market Methods ==========

    async def get_markets(
        self,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[PredictMarket]:
        """
        Get list of markets with pagination.

        Args:
            status: Filter by market status
            limit: Number of markets to fetch
            offset: Pagination offset

        Returns:
            List of PredictMarket objects
        """
        params = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status

        data = await self._request("GET", "/v1/markets", params=params)

        markets = []
        for item in data.get("markets", data.get("data", [])):
            market = self._parse_market(item)
            if market:
                markets.append(market)

        return markets

    async def get_all_markets(
        self,
        status: Optional[str] = None,
        batch_size: int = 100
    ) -> AsyncIterator[PredictMarket]:
        """
        Get all markets with automatic pagination.

        Args:
            status: Filter by market status
            batch_size: Number of markets per request

        Yields:
            PredictMarket objects
        """
        offset = 0
        while True:
            markets = await self.get_markets(
                status=status,
                limit=batch_size,
                offset=offset
            )

            if not markets:
                break

            for market in markets:
                yield market

            if len(markets) < batch_size:
                break

            offset += batch_size

    async def get_market(self, market_id: str) -> Optional[PredictMarket]:
        """
        Get a single market by ID.

        Args:
            market_id: Market ID

        Returns:
            PredictMarket or None
        """
        try:
            data = await self._request("GET", f"/v1/markets/{market_id}")
            return self._parse_market(data)
        except PredictAPIError as e:
            if e.status_code == 404:
                return None
            raise

    def _parse_market(self, data: Dict[str, Any]) -> Optional[PredictMarket]:
        """Parse market data into PredictMarket object."""
        try:
            # Extract polymarket condition IDs
            poly_ids = data.get("polymarketConditionIds", [])
            if isinstance(poly_ids, str):
                poly_ids = [poly_ids] if poly_ids else []

            return PredictMarket(
                market_id=str(data.get("id", data.get("marketId", ""))),
                condition_id=str(data.get("conditionId", "")),
                title=data.get("title", data.get("question", "")),
                slug=data.get("slug", ""),
                category=data.get("category"),
                status=data.get("status", "unknown"),
                decimal_precision=int(data.get("decimalPrecision", 2)),
                fee_rate_bps=int(data.get("feeRateBps", 0)),
                polymarket_condition_ids=poly_ids,
                kalshi_market_ticker=data.get("kalshiMarketTicker"),
                yes_token_id=data.get("yesTokenId", data.get("outcomes", [{}])[0].get("tokenId") if data.get("outcomes") else None),
                no_token_id=data.get("noTokenId", data.get("outcomes", [{}])[1].get("tokenId") if len(data.get("outcomes", [])) > 1 else None),
                raw_data=data
            )
        except Exception as e:
            logger.error(f"Failed to parse market: {e}", data=data)
            return None

    # ========== Order Book Methods ==========

    async def get_orderbook(
        self,
        market_id: str,
        outcome: Optional[Outcome] = None
    ) -> Dict[Outcome, OrderBook]:
        """
        Get order book for a market.

        Note: Predict API returns orderbook in YES price terms.
        NO prices are calculated as complement (1 - YES price).

        Args:
            market_id: Market ID
            outcome: Optional specific outcome

        Returns:
            Dict mapping Outcome to OrderBook
        """
        data = await self._request("GET", f"/v1/markets/{market_id}/orderbook")

        # Parse YES orderbook (directly from API)
        yes_bids = [
            PriceLevel(
                price=safe_decimal(item.get("price")),
                size=safe_decimal(item.get("size", item.get("amount")))
            )
            for item in data.get("bids", [])
        ]
        yes_asks = [
            PriceLevel(
                price=safe_decimal(item.get("price")),
                size=safe_decimal(item.get("size", item.get("amount")))
            )
            for item in data.get("asks", [])
        ]

        yes_book = OrderBook(bids=yes_bids, asks=yes_asks)

        # Calculate NO orderbook (complement prices)
        # NO bid at X = YES ask at (1-X)
        # NO ask at X = YES bid at (1-X)
        precision = int(data.get("decimalPrecision", 2))

        no_bids = [
            PriceLevel(
                price=complement_price(level.price, precision),
                size=level.size
            )
            for level in yes_asks  # YES asks become NO bids
        ]
        no_asks = [
            PriceLevel(
                price=complement_price(level.price, precision),
                size=level.size
            )
            for level in yes_bids  # YES bids become NO asks
        ]

        no_book = OrderBook(bids=no_bids, asks=no_asks)

        result = {Outcome.YES: yes_book, Outcome.NO: no_book}

        if outcome:
            return {outcome: result[outcome]}

        return result

    # ========== Position Methods ==========

    async def get_positions(self) -> List[Position]:
        """
        Get all positions for the authenticated user.

        Returns:
            List of Position objects
        """
        data = await self._request("GET", "/v1/positions")

        positions = []
        for item in data.get("positions", data.get("data", [])):
            position = self._parse_position(item)
            if position:
                positions.append(position)

        return positions

    async def get_position(self, market_id: str) -> Optional[Position]:
        """
        Get position for a specific market.

        Args:
            market_id: Market ID

        Returns:
            Position or None
        """
        positions = await self.get_positions()
        for pos in positions:
            if pos.market_id == market_id:
                return pos
        return Position(market_id=market_id, condition_id="")

    def _parse_position(self, data: Dict[str, Any]) -> Optional[Position]:
        """Parse position data into Position object."""
        try:
            # Handle different response formats
            yes_amount = safe_decimal(data.get("yesAmount", data.get("yes_amount", 0)))
            no_amount = safe_decimal(data.get("noAmount", data.get("no_amount", 0)))

            # Alternative format with outcomes array
            if "outcomes" in data:
                for outcome in data["outcomes"]:
                    if outcome.get("outcome", "").lower() == "yes":
                        yes_amount = safe_decimal(outcome.get("amount", 0))
                    elif outcome.get("outcome", "").lower() == "no":
                        no_amount = safe_decimal(outcome.get("amount", 0))

            return Position(
                market_id=str(data.get("marketId", data.get("market_id", ""))),
                condition_id=str(data.get("conditionId", data.get("condition_id", ""))),
                yes_amount=yes_amount,
                no_amount=no_amount,
                raw_data=data
            )
        except Exception as e:
            logger.error(f"Failed to parse position: {e}", data=data)
            return None

    # ========== Order Methods ==========

    async def get_orders(
        self,
        market_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100
    ) -> List[PredictOrder]:
        """
        Get orders for the authenticated user.

        Args:
            market_id: Optional filter by market
            status: Optional filter by status
            limit: Maximum orders to return

        Returns:
            List of PredictOrder objects
        """
        params = {"limit": limit}
        if market_id:
            params["marketId"] = market_id
        if status:
            params["status"] = status

        data = await self._request("GET", "/v1/orders", params=params)

        orders = []
        for item in data.get("orders", data.get("data", [])):
            order = self._parse_order(item)
            if order:
                orders.append(order)

        return orders

    async def get_open_orders(self, market_id: Optional[str] = None) -> List[PredictOrder]:
        """Get open orders."""
        return await self.get_orders(market_id=market_id, status="open")

    def _parse_order(self, data: Dict[str, Any]) -> Optional[PredictOrder]:
        """Parse order data into PredictOrder object."""
        try:
            outcome_str = data.get("outcome", data.get("side", "")).lower()
            if outcome_str in ("yes", "0"):
                outcome = Outcome.YES
            else:
                outcome = Outcome.NO

            side_str = data.get("type", data.get("orderType", "")).lower()
            if side_str in ("buy", "bid"):
                side = OrderSide.BUY
            else:
                side = OrderSide.SELL

            status_str = data.get("status", "").lower()
            status_map = {
                "pending": OrderStatus.PENDING,
                "open": OrderStatus.OPEN,
                "filled": OrderStatus.FILLED,
                "partially_filled": OrderStatus.PARTIALLY_FILLED,
                "cancelled": OrderStatus.CANCELLED,
                "canceled": OrderStatus.CANCELLED,
                "expired": OrderStatus.EXPIRED,
            }
            status = status_map.get(status_str, OrderStatus.PENDING)

            created_at = data.get("createdAt", data.get("created_at"))
            if isinstance(created_at, str):
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            elif created_at is None:
                created_at = datetime.utcnow()

            return PredictOrder(
                order_id=str(data.get("orderId", data.get("id", ""))),
                market_id=str(data.get("marketId", data.get("market_id", ""))),
                outcome=outcome,
                side=side,
                price=safe_decimal(data.get("price")),
                size=safe_decimal(data.get("size", data.get("amount"))),
                filled_size=safe_decimal(data.get("filledSize", data.get("filled_size", 0))),
                status=status,
                created_at=created_at,
                tx_hash=data.get("txHash", data.get("tx_hash")),
                raw_data=data
            )
        except Exception as e:
            logger.error(f"Failed to parse order: {e}", data=data)
            return None

    async def create_order(
        self,
        market_id: str,
        outcome: Outcome,
        side: OrderSide,
        price: Decimal,
        size: Decimal
    ) -> PredictOrder:
        """
        Create a new limit order.

        Args:
            market_id: Market ID
            outcome: YES or NO
            side: BUY or SELL
            price: Limit price
            size: Order size in shares

        Returns:
            Created PredictOrder

        Raises:
            PredictAPIError: On failure
        """
        payload = {
            "marketId": market_id,
            "outcome": outcome.value.upper(),
            "type": side.value.upper(),
            "price": str(price),
            "size": str(size),
        }

        data = await self._request("POST", "/v1/orders", json=payload)

        order = self._parse_order(data)
        if not order:
            raise PredictAPIError("Failed to parse order response", response=data)

        logger.info(
            "Order created",
            order_id=order.order_id,
            market_id=market_id,
            outcome=outcome.value,
            side=side.value,
            price=str(price),
            size=str(size)
        )

        return order

    async def cancel_order(self, order_id: str, use_fast_removal: bool = False) -> bool:
        """
        Cancel an order.

        Args:
            order_id: Order ID to cancel
            use_fast_removal: Use POST /v1/orders/remove (risky - not on-chain)

        Returns:
            True if successful

        Raises:
            PredictAPIError: On failure
        """
        settings = get_settings()

        if use_fast_removal and settings.use_fast_order_removal:
            # Fast removal - WARNING: not on-chain, order may still be filled!
            logger.warning(
                "Using fast order removal (risky)",
                order_id=order_id
            )
            await self._request("POST", "/v1/orders/remove", json={"orderId": order_id})
        else:
            # Standard cancellation (on-chain)
            await self._request("DELETE", f"/v1/orders/{order_id}")

        logger.info("Order cancelled", order_id=order_id)
        return True

    async def cancel_all_orders(
        self,
        market_id: Optional[str] = None,
        use_fast_removal: bool = False
    ) -> int:
        """
        Cancel all open orders.

        Args:
            market_id: Optional filter by market
            use_fast_removal: Use fast removal (risky)

        Returns:
            Number of orders cancelled
        """
        orders = await self.get_open_orders(market_id=market_id)

        cancelled = 0
        for order in orders:
            try:
                await self.cancel_order(order.order_id, use_fast_removal)
                cancelled += 1
            except PredictAPIError as e:
                logger.error(f"Failed to cancel order {order.order_id}: {e}")

        return cancelled

    # ========== Match Methods ==========

    async def get_matches(
        self,
        market_id: Optional[str] = None,
        after: Optional[datetime] = None,
        limit: int = 100
    ) -> List[MatchEvent]:
        """
        Get trade match events.

        Args:
            market_id: Optional filter by market
            after: Optional filter for matches after this time
            limit: Maximum matches to return

        Returns:
            List of MatchEvent objects
        """
        params = {"limit": limit}
        if market_id:
            params["marketId"] = market_id
        if after:
            params["after"] = after.isoformat()

        data = await self._request("GET", "/v1/orders/matches", params=params)

        matches = []
        for item in data.get("matches", data.get("data", [])):
            match = self._parse_match(item)
            if match:
                matches.append(match)

        return matches

    def _parse_match(self, data: Dict[str, Any]) -> Optional[MatchEvent]:
        """Parse match data into MatchEvent object."""
        try:
            outcome_str = data.get("outcome", "").lower()
            outcome = Outcome.YES if outcome_str == "yes" else Outcome.NO

            side_str = data.get("type", data.get("side", "")).lower()
            side = OrderSide.BUY if side_str in ("buy", "bid") else OrderSide.SELL

            executed_at = data.get("executedAt", data.get("executed_at"))
            if isinstance(executed_at, str):
                executed_at = datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
            else:
                executed_at = datetime.utcnow()

            return MatchEvent(
                match_id=str(data.get("matchId", data.get("id", ""))),
                order_id=str(data.get("orderId", data.get("order_id", ""))),
                market_id=str(data.get("marketId", data.get("market_id", ""))),
                outcome=outcome,
                side=side,
                price=safe_decimal(data.get("price")),
                size=safe_decimal(data.get("size", data.get("amount"))),
                executed_at=executed_at,
                raw_data=data
            )
        except Exception as e:
            logger.error(f"Failed to parse match: {e}", data=data)
            return None

    # ========== Merge Methods ==========

    async def merge_positions(
        self,
        condition_id: str,
        amount: Decimal
    ) -> bool:
        """
        Merge YES and NO positions back into collateral.

        Args:
            condition_id: Market condition ID
            amount: Amount to merge (min of YES/NO positions)

        Returns:
            True if successful

        Raises:
            PredictAPIError: On failure
        """
        payload = {
            "conditionId": condition_id,
            "amount": str(amount),
        }

        await self._request("POST", "/v1/positions/merge", json=payload)

        logger.info(
            "Positions merged",
            condition_id=condition_id,
            amount=str(amount)
        )

        return True
