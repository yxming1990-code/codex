"""
External price feed engine.

Handles:
- Polling external markets (Polymarket/Kalshi) for prices
- Tracking price history and changes
- Detecting price jumps and failures
"""

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field

from ..config import get_settings, ExternalSource
from ..api import (
    PolymarketClient,
    KalshiClient,
    PolymarketAPIError,
    KalshiAPIError,
)
from ..market import MarketMapping
from ..utils import (
    get_logger,
    ExternalQuote,
    price_jump_exceeded,
    retry_with_backoff,
)

logger = get_logger(__name__)


@dataclass
class PriceFeedState:
    """State for a single price feed."""
    market_id: str
    mapping: MarketMapping
    current_quote: Optional[ExternalQuote] = None
    previous_quote: Optional[ExternalQuote] = None
    last_update: Optional[datetime] = None
    consecutive_failures: int = 0
    last_error: Optional[str] = None

    # Price jump detection
    last_yes_ask: Optional[Decimal] = None
    last_no_ask: Optional[Decimal] = None

    @property
    def is_stale(self) -> bool:
        """Check if quote is stale (older than 2x poll interval)."""
        if not self.last_update:
            return True
        settings = get_settings()
        stale_threshold = settings.polling.external_price_interval * 2
        return (datetime.utcnow() - self.last_update).total_seconds() > stale_threshold

    def update_quote(self, quote: ExternalQuote) -> None:
        """Update with new quote."""
        self.previous_quote = self.current_quote
        self.current_quote = quote
        self.last_update = datetime.utcnow()
        self.consecutive_failures = 0
        self.last_error = None

        # Track last prices for jump detection
        if quote.yes_ask is not None:
            self.last_yes_ask = quote.yes_ask
        if quote.no_ask is not None:
            self.last_no_ask = quote.no_ask

    def record_failure(self, error: str) -> None:
        """Record a fetch failure."""
        self.consecutive_failures += 1
        self.last_error = error

    def check_price_jump(self, new_quote: ExternalQuote, threshold: Decimal) -> Optional[str]:
        """
        Check if new quote has a price jump exceeding threshold.

        Returns:
            Reason string if jump detected, None otherwise
        """
        if self.last_yes_ask is not None and new_quote.yes_ask is not None:
            if price_jump_exceeded(self.last_yes_ask, new_quote.yes_ask, threshold):
                return f"YES ask jumped from {self.last_yes_ask} to {new_quote.yes_ask}"

        if self.last_no_ask is not None and new_quote.no_ask is not None:
            if price_jump_exceeded(self.last_no_ask, new_quote.no_ask, threshold):
                return f"NO ask jumped from {self.last_no_ask} to {new_quote.no_ask}"

        return None


# Callback types
PriceUpdateCallback = Callable[[str, ExternalQuote], None]
PriceJumpCallback = Callable[[str, str], None]  # market_id, reason
FetchFailureCallback = Callable[[str, int, str], None]  # market_id, failures, error


class PriceFeedEngine:
    """
    External price feed engine.

    Polls external markets for prices and tracks state.
    Triggers callbacks on updates, jumps, and failures.
    """

    def __init__(
        self,
        polymarket_client: Optional[PolymarketClient] = None,
        kalshi_client: Optional[KalshiClient] = None
    ):
        """
        Initialize price feed engine.

        Args:
            polymarket_client: Polymarket client for price fetching
            kalshi_client: Kalshi client for price fetching
        """
        self._polymarket = polymarket_client
        self._kalshi = kalshi_client

        # State per market
        self._feeds: Dict[str, PriceFeedState] = {}

        # Callbacks
        self._on_price_update: List[PriceUpdateCallback] = []
        self._on_price_jump: List[PriceJumpCallback] = []
        self._on_fetch_failure: List[FetchFailureCallback] = []

        # Control
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    def add_feed(self, mapping: MarketMapping) -> None:
        """
        Add a market to the price feed.

        Args:
            mapping: Market mapping to add
        """
        market_id = mapping.market_id
        if market_id not in self._feeds:
            self._feeds[market_id] = PriceFeedState(
                market_id=market_id,
                mapping=mapping
            )
            logger.info(f"Added price feed for {market_id}")

    def remove_feed(self, market_id: str) -> None:
        """
        Remove a market from the price feed.

        Args:
            market_id: Market ID to remove
        """
        if market_id in self._feeds:
            del self._feeds[market_id]
            logger.info(f"Removed price feed for {market_id}")

    def get_feed(self, market_id: str) -> Optional[PriceFeedState]:
        """Get feed state for a market."""
        return self._feeds.get(market_id)

    def get_quote(self, market_id: str) -> Optional[ExternalQuote]:
        """Get current quote for a market."""
        feed = self._feeds.get(market_id)
        return feed.current_quote if feed else None

    # Callback registration
    def on_price_update(self, callback: PriceUpdateCallback) -> None:
        """Register callback for price updates."""
        self._on_price_update.append(callback)

    def on_price_jump(self, callback: PriceJumpCallback) -> None:
        """Register callback for price jumps."""
        self._on_price_jump.append(callback)

    def on_fetch_failure(self, callback: FetchFailureCallback) -> None:
        """Register callback for fetch failures."""
        self._on_fetch_failure.append(callback)

    async def fetch_quote(self, market_id: str) -> Optional[ExternalQuote]:
        """
        Fetch quote for a single market.

        Args:
            market_id: Market ID to fetch

        Returns:
            ExternalQuote or None on failure
        """
        feed = self._feeds.get(market_id)
        if not feed:
            return None

        mapping = feed.mapping
        settings = get_settings()

        try:
            quote = None

            if mapping.external_source == ExternalSource.POLYMARKET:
                if self._polymarket:
                    quote = await retry_with_backoff(
                        self._polymarket.get_quote,
                        mapping.external_id,
                        max_attempts=2,  # Fewer retries for polling
                        base_delay=1.0,
                        retry_on=(PolymarketAPIError,)
                    )

            elif mapping.external_source == ExternalSource.KALSHI:
                if self._kalshi:
                    quote = await retry_with_backoff(
                        self._kalshi.get_quote,
                        mapping.external_id,
                        max_attempts=2,
                        base_delay=1.0,
                        retry_on=(KalshiAPIError,)
                    )

            if quote:
                # Check for price jump before updating
                jump_reason = feed.check_price_jump(quote, settings.risk.jump_threshold)
                if jump_reason:
                    logger.warning(f"Price jump detected: {jump_reason}", market_id=market_id)
                    for callback in self._on_price_jump:
                        try:
                            callback(market_id, jump_reason)
                        except Exception as e:
                            logger.error(f"Price jump callback error: {e}")

                # Update feed state
                feed.update_quote(quote)

                # Trigger update callbacks
                for callback in self._on_price_update:
                    try:
                        callback(market_id, quote)
                    except Exception as e:
                        logger.error(f"Price update callback error: {e}")

                return quote

            else:
                feed.record_failure("No quote returned")
                self._trigger_failure_callbacks(feed)
                return None

        except Exception as e:
            error_msg = str(e)
            feed.record_failure(error_msg)
            self._trigger_failure_callbacks(feed)
            logger.error(f"Failed to fetch quote: {e}", market_id=market_id)
            return None

    def _trigger_failure_callbacks(self, feed: PriceFeedState) -> None:
        """Trigger failure callbacks."""
        for callback in self._on_fetch_failure:
            try:
                callback(feed.market_id, feed.consecutive_failures, feed.last_error or "Unknown")
            except Exception as e:
                logger.error(f"Fetch failure callback error: {e}")

    async def poll_all(self) -> Dict[str, Optional[ExternalQuote]]:
        """
        Poll all active feeds for quotes.

        Returns:
            Dict mapping market_id to quote (or None on failure)
        """
        results = {}

        # Create tasks for parallel fetching
        tasks = []
        market_ids = []

        for market_id in self._feeds:
            tasks.append(self.fetch_quote(market_id))
            market_ids.append(market_id)

        if tasks:
            quotes = await asyncio.gather(*tasks, return_exceptions=True)

            for market_id, quote in zip(market_ids, quotes):
                if isinstance(quote, Exception):
                    results[market_id] = None
                else:
                    results[market_id] = quote

        return results

    async def start_polling(self) -> None:
        """Start the price polling loop."""
        if self._running:
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Price feed polling started")

    async def stop_polling(self) -> None:
        """Stop the price polling loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("Price feed polling stopped")

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        settings = get_settings()
        interval = settings.polling.external_price_interval

        while self._running:
            try:
                await self.poll_all()
            except Exception as e:
                logger.error(f"Poll loop error: {e}")

            await asyncio.sleep(interval)

    @property
    def feed_count(self) -> int:
        """Number of active feeds."""
        return len(self._feeds)

    @property
    def is_running(self) -> bool:
        """Whether polling is active."""
        return self._running

    def get_all_quotes(self) -> Dict[str, ExternalQuote]:
        """Get all current quotes."""
        return {
            market_id: feed.current_quote
            for market_id, feed in self._feeds.items()
            if feed.current_quote is not None
        }

    def get_stale_feeds(self) -> List[str]:
        """Get market IDs with stale data."""
        return [
            market_id
            for market_id, feed in self._feeds.items()
            if feed.is_stale
        ]
