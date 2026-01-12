"""
Polymarket API client for fetching external price data.

Polymarket uses a CLOB (Central Limit Order Book) for its binary markets.
YES + NO prices sum to $1 (complement relationship).
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
    complement_price,
)

logger = get_logger(__name__)


class PolymarketAPIError(Exception):
    """Polymarket API error."""

    def __init__(self, message: str, status_code: int = 0, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class PolymarketClient:
    """
    Async client for Polymarket CLOB API.

    Uses the public CLOB and Gamma APIs to fetch:
    - Market metadata
    - Order book / prices
    - Token IDs for YES/NO outcomes
    """

    def __init__(self, base_url: Optional[str] = None):
        """
        Initialize Polymarket client.

        Args:
            base_url: Optional base URL override
        """
        settings = get_settings()
        self._clob_url = base_url or settings.polymarket_api.clob_base_url
        self._gamma_url = settings.polymarket_api.gamma_base_url
        self._timeout = settings.polymarket_api.timeout
        self._rate_limiter = get_external_limiter()
        self._client: Optional[httpx.AsyncClient] = None

        # Cache for condition_id -> token_id mapping
        self._token_cache: Dict[str, Dict[str, str]] = {}

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        base_url: str,
        endpoint: str,
        params: Optional[Dict] = None,
        **kwargs
    ) -> Any:
        """
        Make a rate-limited API request.

        Args:
            base_url: Base URL (CLOB or Gamma)
            endpoint: API endpoint
            params: Query parameters
            **kwargs: Additional httpx kwargs

        Returns:
            Response JSON

        Raises:
            PolymarketAPIError: On API error
        """
        await self._rate_limiter.acquire()

        if not self._client:
            raise PolymarketAPIError("Client not initialized. Use async context manager.")

        url = f"{base_url}{endpoint}"

        try:
            response = await self._client.get(url, params=params, **kwargs)

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "5"))
                logger.warning(f"Polymarket rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                raise PolymarketAPIError("Rate limited", 429, None)

            if response.status_code >= 400:
                error_body = response.text
                try:
                    error_body = response.json()
                except:
                    pass
                raise PolymarketAPIError(
                    f"API error: {response.status_code}",
                    response.status_code,
                    error_body
                )

            return response.json()

        except httpx.RequestError as e:
            raise PolymarketAPIError(f"Request failed: {e}")

    async def get_market_by_condition(self, condition_id: str) -> Optional[Dict[str, Any]]:
        """
        Get market info by condition ID.

        Args:
            condition_id: Polymarket condition ID

        Returns:
            Market data dict or None
        """
        try:
            # Try Gamma API first for market lookup
            data = await self._request(
                self._gamma_url,
                "/markets",
                params={"condition_id": condition_id}
            )

            if isinstance(data, list) and len(data) > 0:
                return data[0]

            return None

        except PolymarketAPIError as e:
            if e.status_code == 404:
                return None
            raise

    async def get_token_ids(self, condition_id: str) -> Optional[Dict[str, str]]:
        """
        Get YES/NO token IDs for a condition.

        Args:
            condition_id: Polymarket condition ID

        Returns:
            Dict with 'yes' and 'no' token IDs, or None
        """
        # Check cache first
        if condition_id in self._token_cache:
            return self._token_cache[condition_id]

        market = await self.get_market_by_condition(condition_id)
        if not market:
            return None

        # Extract token IDs from market data
        # Polymarket returns clobTokenIds in market response
        clob_token_ids = market.get("clobTokenIds", [])
        tokens = market.get("tokens", [])

        token_ids = {}

        # Try clobTokenIds array
        if len(clob_token_ids) >= 2:
            token_ids["yes"] = str(clob_token_ids[0])
            token_ids["no"] = str(clob_token_ids[1])
        # Try tokens array
        elif tokens:
            for token in tokens:
                outcome = token.get("outcome", "").lower()
                if outcome in ("yes", "no"):
                    token_ids[outcome] = str(token.get("token_id", ""))

        if token_ids:
            self._token_cache[condition_id] = token_ids
            return token_ids

        return None

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """
        Get order book for a specific token.

        Args:
            token_id: CLOB token ID

        Returns:
            OrderBook with bids and asks
        """
        data = await self._request(
            self._clob_url,
            f"/book",
            params={"token_id": token_id}
        )

        bids = [
            PriceLevel(
                price=safe_decimal(level.get("price")),
                size=safe_decimal(level.get("size"))
            )
            for level in data.get("bids", [])
        ]

        asks = [
            PriceLevel(
                price=safe_decimal(level.get("price")),
                size=safe_decimal(level.get("size"))
            )
            for level in data.get("asks", [])
        ]

        return OrderBook(bids=bids, asks=asks)

    async def get_price(self, token_id: str) -> Optional[Dict[str, Decimal]]:
        """
        Get best bid/ask prices for a token.

        Args:
            token_id: CLOB token ID

        Returns:
            Dict with 'bid' and 'ask' prices, or None
        """
        try:
            data = await self._request(
                self._clob_url,
                f"/price",
                params={"token_id": token_id}
            )

            return {
                "bid": safe_decimal(data.get("bid")),
                "ask": safe_decimal(data.get("ask")),
            }

        except PolymarketAPIError:
            return None

    async def get_quote(self, condition_id: str) -> Optional[ExternalQuote]:
        """
        Get external quote for a Polymarket market.

        Args:
            condition_id: Polymarket condition ID

        Returns:
            ExternalQuote or None if market not found
        """
        token_ids = await self.get_token_ids(condition_id)
        if not token_ids:
            logger.warning(f"No token IDs found for condition {condition_id}")
            return None

        yes_token_id = token_ids.get("yes")
        no_token_id = token_ids.get("no")

        quote = ExternalQuote(
            source="polymarket",
            market_id=condition_id,
            timestamp=datetime.utcnow()
        )

        # Get YES prices
        if yes_token_id:
            yes_prices = await self.get_price(yes_token_id)
            if yes_prices:
                quote.yes_bid = yes_prices.get("bid")
                quote.yes_ask = yes_prices.get("ask")

        # Get NO prices (or derive from YES)
        if no_token_id:
            no_prices = await self.get_price(no_token_id)
            if no_prices:
                quote.no_bid = no_prices.get("bid")
                quote.no_ask = no_prices.get("ask")

        # If we have YES but not NO, calculate complement
        if quote.yes_ask is not None and quote.no_ask is None:
            quote.no_bid = complement_price(quote.yes_ask, 2) if quote.yes_ask else None
            quote.no_ask = complement_price(quote.yes_bid, 2) if quote.yes_bid else None

        if not quote.is_valid():
            logger.warning(f"Invalid quote for condition {condition_id}")
            return None

        return quote

    async def validate_mapping(self, condition_id: str) -> bool:
        """
        Validate that a condition ID can be mapped to Polymarket prices.

        Args:
            condition_id: Polymarket condition ID

        Returns:
            True if mapping is valid and prices can be fetched
        """
        try:
            quote = await self.get_quote(condition_id)
            return quote is not None and quote.is_valid()
        except Exception as e:
            logger.error(f"Mapping validation failed: {e}", condition_id=condition_id)
            return False


async def fetch_polymarket_quote(condition_id: str) -> Optional[ExternalQuote]:
    """
    Convenience function to fetch a Polymarket quote.

    Args:
        condition_id: Polymarket condition ID

    Returns:
        ExternalQuote or None
    """
    settings = get_settings()

    async with PolymarketClient() as client:
        return await retry_with_backoff(
            client.get_quote,
            condition_id,
            max_attempts=settings.rate_limit.retry_max_attempts,
            base_delay=settings.rate_limit.retry_base_delay,
            retry_on=(PolymarketAPIError, httpx.RequestError)
        )
