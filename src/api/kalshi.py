"""
Kalshi API client for fetching external price data.

Important Kalshi price conventions:
- Prices are in CENTS (1-99)
- Order book only returns BIDS (YES bids + NO bids)
- YES bid @ X = NO ask @ (100-X)
- NO bid @ X = YES ask @ (100-X)
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, List, Dict, Any

import httpx

from ..config import get_settings
from ..utils import (
    get_logger,
    get_external_limiter,
    retry_with_backoff,
    ExternalQuote,
    OrderBook,
    PriceLevel,
    safe_decimal,
    kalshi_cents_to_decimal,
    complement_price,
)

logger = get_logger(__name__)


class KalshiAPIError(Exception):
    """Kalshi API error."""

    def __init__(self, message: str, status_code: int = 0, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class KalshiClient:
    """
    Async client for Kalshi API.

    Uses the public trade API to fetch:
    - Market metadata
    - Order book (bids only - asks derived via complement)
    """

    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize Kalshi client.

        Args:
            base_url: Optional base URL override
        """
        settings = get_settings()
        self._base_url = base_url or settings.kalshi_api.base_url
        self._timeout = settings.kalshi_api.timeout
        self._rate_limiter = get_external_limiter()
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        **kwargs
    ) -> Any:
        """
        Make a rate-limited API request.

        Args:
            endpoint: API endpoint
            params: Query parameters
            **kwargs: Additional httpx kwargs

        Returns:
            Response JSON

        Raises:
            KalshiAPIError: On API error
        """
        await self._rate_limiter.acquire()

        if not self._client:
            raise KalshiAPIError("Client not initialized. Use async context manager.")

        try:
            response = await self._client.get(endpoint, params=params, **kwargs)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                logger.warning(f"Kalshi rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                raise KalshiAPIError("Rate limited", 429, None)

            if response.status_code >= 400:
                error_body = response.text
                try:
                    error_body = response.json()
                except:
                    pass
                raise KalshiAPIError(
                    f"API error: {response.status_code}",
                    response.status_code,
                    error_body
                )

            return response.json()

        except httpx.RequestError as e:
            raise KalshiAPIError(f"Request failed: {e}")

    async def get_market(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Get market info by ticker.

        Args:
            ticker: Kalshi market ticker

        Returns:
            Market data dict or None
        """
        try:
            data = await self._request(f"/markets/{ticker}")
            return data.get("market", data)
        except KalshiAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_orderbook(self, ticker: str) -> Dict[str, OrderBook]:
        """
        Get order book for a Kalshi market.

        Kalshi returns only bids. We compute asks using:
        - YES ask @ X = NO bid @ (100-X)
        - NO ask @ X = YES bid @ (100-X)

        Args:
            ticker: Market ticker

        Returns:
            Dict mapping 'yes' and 'no' to OrderBook
        """
        data = await self._request(f"/markets/{ticker}/orderbook")

        orderbook = data.get("orderbook", data)

        # Parse YES bids (in cents)
        yes_bids_raw = orderbook.get("yes", [])
        no_bids_raw = orderbook.get("no", [])

        # Convert to decimal and build order books
        yes_bids = []
        for level in yes_bids_raw:
            price_cents = level[0] if isinstance(level, list) else level.get("price", 0)
            size = level[1] if isinstance(level, list) else level.get("quantity", 0)
            yes_bids.append(PriceLevel(
                price=kalshi_cents_to_decimal(int(price_cents)),
                size=safe_decimal(size)
            ))

        no_bids = []
        for level in no_bids_raw:
            price_cents = level[0] if isinstance(level, list) else level.get("price", 0)
            size = level[1] if isinstance(level, list) else level.get("quantity", 0)
            no_bids.append(PriceLevel(
                price=kalshi_cents_to_decimal(int(price_cents)),
                size=safe_decimal(size)
            ))

        # Calculate asks from opposing bids
        # YES ask @ X = NO bid @ (100-X)
        yes_asks = [
            PriceLevel(
                price=complement_price(level.price, 2),
                size=level.size
            )
            for level in no_bids
        ]

        # NO ask @ X = YES bid @ (100-X)
        no_asks = [
            PriceLevel(
                price=complement_price(level.price, 2),
                size=level.size
            )
            for level in yes_bids
        ]

        return {
            "yes": OrderBook(bids=yes_bids, asks=yes_asks),
            "no": OrderBook(bids=no_bids, asks=no_asks),
        }

    async def get_quote(self, ticker: str) -> Optional[ExternalQuote]:
        """
        Get external quote for a Kalshi market.

        Args:
            ticker: Kalshi market ticker

        Returns:
            ExternalQuote or None if market not found
        """
        try:
            orderbooks = await self.get_orderbook(ticker)
        except KalshiAPIError as e:
            if e.status_code == 404:
                return None
            raise

        yes_book = orderbooks.get("yes", OrderBook())
        no_book = orderbooks.get("no", OrderBook())

        quote = ExternalQuote(
            source="kalshi",
            market_id=ticker,
            yes_bid=yes_book.best_bid,
            yes_ask=yes_book.best_ask,
            no_bid=no_book.best_bid,
            no_ask=no_book.best_ask,
            timestamp=datetime.utcnow()
        )

        if not quote.is_valid():
            logger.warning(f"Invalid quote for ticker {ticker}")
            return None

        return quote

    async def validate_mapping(self, ticker: str) -> bool:
        """
        Validate that a ticker can be mapped to Kalshi prices.

        Args:
            ticker: Kalshi market ticker

        Returns:
            True if mapping is valid and prices can be fetched
        """
        try:
            market = await self.get_market(ticker)
            if not market:
                return False

            # Check market status
            status = market.get("status", "").lower()
            if status not in ("open", "active"):
                logger.warning(f"Kalshi market {ticker} status: {status}")
                return False

            # Try to get a quote
            quote = await self.get_quote(ticker)
            return quote is not None and quote.is_valid()

        except Exception as e:
            logger.error(f"Mapping validation failed: {e}", ticker=ticker)
            return False


async def fetch_kalshi_quote(ticker: str) -> Optional[ExternalQuote]:
    """
    Convenience function to fetch a Kalshi quote.

    Args:
        ticker: Kalshi market ticker

    Returns:
        ExternalQuote or None
    """
    settings = get_settings()

    async with KalshiClient() as client:
        return await retry_with_backoff(
            client.get_quote,
            ticker,
            max_attempts=settings.rate_limit.retry_max_attempts,
            base_delay=settings.rate_limit.retry_base_delay,
            retry_on=(KalshiAPIError, httpx.RequestError)
        )
