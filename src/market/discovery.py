"""
Market discovery and mapping module.

Handles:
- Fetching Predict markets
- Filtering by allowlist and tradability
- Mapping to external markets (Polymarket/Kalshi)
- Validation of external mappings
"""

import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Set, Tuple

from ..config import get_settings, ExternalSource
from ..api import (
    PredictClient,
    PolymarketClient,
    KalshiClient,
)
from ..utils import (
    get_logger,
    PredictMarket,
    ExternalQuote,
)

logger = get_logger(__name__)


class MarketMapping:
    """Represents a validated mapping between Predict and external market."""

    def __init__(
        self,
        predict_market: PredictMarket,
        external_source: ExternalSource,
        external_id: str,
        validated: bool = False,
        last_validation: Optional[datetime] = None
    ):
        self.predict_market = predict_market
        self.external_source = external_source
        self.external_id = external_id
        self.validated = validated
        self.last_validation = last_validation

    @property
    def market_id(self) -> str:
        return self.predict_market.market_id

    @property
    def title(self) -> str:
        return self.predict_market.title


class MarketDiscovery:
    """
    Market discovery and mapping service.

    Responsibilities:
    - Fetch all tradable markets from Predict
    - Filter by allowlist
    - Create and validate external market mappings
    """

    def __init__(
        self,
        predict_client: PredictClient,
        polymarket_client: Optional[PolymarketClient] = None,
        kalshi_client: Optional[KalshiClient] = None
    ):
        """
        Initialize market discovery.

        Args:
            predict_client: Predict API client
            polymarket_client: Optional Polymarket client for validation
            kalshi_client: Optional Kalshi client for validation
        """
        self._predict = predict_client
        self._polymarket = polymarket_client
        self._kalshi = kalshi_client

        # Cache
        self._markets: Dict[str, PredictMarket] = {}
        self._mappings: Dict[str, MarketMapping] = {}
        self._last_refresh: Optional[datetime] = None

    async def refresh_markets(
        self,
        status_filter: Optional[str] = None
    ) -> List[PredictMarket]:
        """
        Refresh the list of markets from Predict.

        Args:
            status_filter: Optional status to filter by

        Returns:
            List of all fetched markets
        """
        logger.info("Refreshing market list from Predict")

        markets = []
        async for market in self._predict.get_all_markets(status=status_filter):
            markets.append(market)
            self._markets[market.market_id] = market

        self._last_refresh = datetime.utcnow()
        logger.info(f"Fetched {len(markets)} markets from Predict")

        return markets

    def get_tradable_markets(
        self,
        allowlist: Optional[List[str]] = None,
        require_external_mapping: bool = True
    ) -> List[PredictMarket]:
        """
        Get markets that can be traded.

        Args:
            allowlist: Optional list of market IDs/slugs to include
            require_external_mapping: Only include markets with external mapping

        Returns:
            List of tradable markets
        """
        settings = get_settings()

        # Use settings allowlist if not provided
        if allowlist is None:
            allowlist = settings.market_allowlist

        allowlist_set = set(allowlist) if allowlist else None

        tradable = []
        for market in self._markets.values():
            # Check status (should be tradable)
            if market.status.lower() not in ("active", "open", "registered", "trading"):
                continue

            # Check external mapping if required
            if require_external_mapping and not market.has_external_mapping:
                continue

            # Check allowlist if provided
            if allowlist_set:
                if market.market_id not in allowlist_set and \
                   market.slug not in allowlist_set and \
                   market.condition_id not in allowlist_set:
                    continue

            tradable.append(market)

        return tradable

    def get_mappable_markets(self) -> Tuple[List[PredictMarket], List[PredictMarket]]:
        """
        Categorize markets by mapping availability.

        Returns:
            Tuple of (mappable_markets, unmappable_markets)
        """
        mappable = []
        unmappable = []

        for market in self._markets.values():
            if market.has_external_mapping:
                mappable.append(market)
            else:
                unmappable.append(market)

        return mappable, unmappable

    def create_mapping(self, market: PredictMarket) -> Optional[MarketMapping]:
        """
        Create a mapping for a Predict market.

        Args:
            market: Predict market

        Returns:
            MarketMapping or None if no external source available
        """
        if market.polymarket_condition_ids:
            # Use first condition ID for Polymarket
            external_id = market.polymarket_condition_ids[0]
            mapping = MarketMapping(
                predict_market=market,
                external_source=ExternalSource.POLYMARKET,
                external_id=external_id
            )
        elif market.kalshi_market_ticker:
            mapping = MarketMapping(
                predict_market=market,
                external_source=ExternalSource.KALSHI,
                external_id=market.kalshi_market_ticker
            )
        else:
            return None

        self._mappings[market.market_id] = mapping
        return mapping

    async def validate_mapping(self, mapping: MarketMapping) -> bool:
        """
        Validate that a mapping can fetch external prices.

        Args:
            mapping: MarketMapping to validate

        Returns:
            True if validation successful
        """
        try:
            if mapping.external_source == ExternalSource.POLYMARKET:
                if not self._polymarket:
                    logger.warning("No Polymarket client for validation")
                    return False
                valid = await self._polymarket.validate_mapping(mapping.external_id)

            elif mapping.external_source == ExternalSource.KALSHI:
                if not self._kalshi:
                    logger.warning("No Kalshi client for validation")
                    return False
                valid = await self._kalshi.validate_mapping(mapping.external_id)

            else:
                valid = False

            mapping.validated = valid
            mapping.last_validation = datetime.utcnow()

            logger.info(
                f"Mapping validation {'succeeded' if valid else 'failed'}",
                market_id=mapping.market_id,
                external_source=mapping.external_source.value,
                external_id=mapping.external_id
            )

            return valid

        except Exception as e:
            logger.error(f"Mapping validation error: {e}", mapping=mapping.market_id)
            mapping.validated = False
            mapping.last_validation = datetime.utcnow()
            return False

    async def validate_all_mappings(self) -> Dict[str, bool]:
        """
        Validate all created mappings.

        Returns:
            Dict mapping market_id to validation result
        """
        results = {}

        for market_id, mapping in self._mappings.items():
            results[market_id] = await self.validate_mapping(mapping)
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)

        return results

    def get_mapping(self, market_id: str) -> Optional[MarketMapping]:
        """Get mapping for a market ID."""
        return self._mappings.get(market_id)

    def get_validated_mappings(self) -> List[MarketMapping]:
        """Get all validated mappings."""
        return [m for m in self._mappings.values() if m.validated]

    def get_market(self, market_id: str) -> Optional[PredictMarket]:
        """Get market by ID."""
        return self._markets.get(market_id)

    @property
    def market_count(self) -> int:
        """Total number of cached markets."""
        return len(self._markets)

    @property
    def mapping_count(self) -> int:
        """Total number of created mappings."""
        return len(self._mappings)

    @property
    def validated_mapping_count(self) -> int:
        """Number of validated mappings."""
        return len(self.get_validated_mappings())


async def discover_and_map_markets(
    allowlist: Optional[List[str]] = None
) -> Tuple[MarketDiscovery, List[MarketMapping]]:
    """
    Convenience function to discover markets and create mappings.

    Args:
        allowlist: Optional market allowlist

    Returns:
        Tuple of (MarketDiscovery instance, list of validated mappings)
    """
    async with PredictClient() as predict, \
               PolymarketClient() as polymarket, \
               KalshiClient() as kalshi:

        discovery = MarketDiscovery(predict, polymarket, kalshi)

        # Refresh markets
        await discovery.refresh_markets()

        # Get tradable markets
        tradable = discovery.get_tradable_markets(allowlist=allowlist)

        # Create mappings
        for market in tradable:
            discovery.create_mapping(market)

        # Validate all
        await discovery.validate_all_mappings()

        return discovery, discovery.get_validated_mappings()
