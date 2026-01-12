"""
Position tracking and auto-merge module.

Handles:
- Position monitoring for all markets
- Detection of dual-side positions (YES + NO)
- Automatic position merging
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, field

from ..config import get_settings
from ..api import PredictClient, PredictAPIError
from ..market import MarketDiscovery
from ..utils import (
    get_logger,
    Position,
)

logger = get_logger(__name__)


@dataclass
class MergeResult:
    """Result of a merge operation."""
    market_id: str
    condition_id: str
    amount: Decimal
    success: bool
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


# Callback types
PositionUpdateCallback = Callable[[str, Position], None]
MergeCallback = Callable[[MergeResult], None]


class PositionTracker:
    """
    Position tracker with auto-merge capability.

    Responsibilities:
    - Track positions for all registered markets
    - Detect when both YES and NO positions exist
    - Trigger automatic merge when conditions are met
    """

    def __init__(
        self,
        predict_client: PredictClient,
        market_discovery: Optional[MarketDiscovery] = None
    ):
        """
        Initialize position tracker.

        Args:
            predict_client: Predict API client
            market_discovery: Optional market discovery for condition IDs
        """
        self._predict = predict_client
        self._discovery = market_discovery

        # Position cache per market
        self._positions: Dict[str, Position] = {}
        self._last_update: Optional[datetime] = None

        # Registered markets
        self._tracked_markets: Dict[str, str] = {}  # market_id -> condition_id

        # Merge history
        self._merge_history: List[MergeResult] = []

        # Callbacks
        self._on_position_update: List[PositionUpdateCallback] = []
        self._on_merge: List[MergeCallback] = []

        # Control
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    def register_market(self, market_id: str, condition_id: str) -> None:
        """
        Register a market for position tracking.

        Args:
            market_id: Market ID
            condition_id: Market condition ID (needed for merge)
        """
        self._tracked_markets[market_id] = condition_id

    def unregister_market(self, market_id: str) -> None:
        """Unregister a market."""
        if market_id in self._tracked_markets:
            del self._tracked_markets[market_id]
        if market_id in self._positions:
            del self._positions[market_id]

    # Callback registration
    def on_position_update(self, callback: PositionUpdateCallback) -> None:
        """Register callback for position updates."""
        self._on_position_update.append(callback)

    def on_merge(self, callback: MergeCallback) -> None:
        """Register callback for merge events."""
        self._on_merge.append(callback)

    async def refresh_positions(self) -> Dict[str, Position]:
        """
        Refresh all positions from Predict API.

        Returns:
            Dict mapping market_id to Position
        """
        try:
            positions = await self._predict.get_positions()

            # Index by market ID
            position_map: Dict[str, Position] = {}
            for pos in positions:
                position_map[pos.market_id] = pos

            # Update cache for tracked markets
            for market_id in self._tracked_markets:
                if market_id in position_map:
                    old_pos = self._positions.get(market_id)
                    new_pos = position_map[market_id]
                    self._positions[market_id] = new_pos

                    # Trigger callbacks if changed
                    if old_pos is None or \
                       old_pos.yes_amount != new_pos.yes_amount or \
                       old_pos.no_amount != new_pos.no_amount:
                        for callback in self._on_position_update:
                            try:
                                callback(market_id, new_pos)
                            except Exception as e:
                                logger.error(f"Position update callback error: {e}")
                else:
                    # No position - create empty
                    self._positions[market_id] = Position(
                        market_id=market_id,
                        condition_id=self._tracked_markets.get(market_id, "")
                    )

            self._last_update = datetime.utcnow()
            return self._positions

        except PredictAPIError as e:
            logger.error(f"Failed to refresh positions: {e}")
            return self._positions

    def get_position(self, market_id: str) -> Optional[Position]:
        """Get cached position for a market."""
        return self._positions.get(market_id)

    def get_all_positions(self) -> Dict[str, Position]:
        """Get all cached positions."""
        return self._positions.copy()

    def get_mergeable_positions(self, min_amount: Decimal = Decimal("1")) -> List[Position]:
        """
        Get positions that can be merged (have both YES and NO).

        Args:
            min_amount: Minimum mergeable amount to include

        Returns:
            List of positions with mergeable amounts >= min_amount
        """
        mergeable = []
        for pos in self._positions.values():
            if pos.has_both_sides and pos.mergeable_amount >= min_amount:
                mergeable.append(pos)
        return mergeable

    async def merge_position(
        self,
        market_id: str,
        amount: Optional[Decimal] = None
    ) -> MergeResult:
        """
        Merge a position to reclaim collateral.

        Args:
            market_id: Market ID
            amount: Amount to merge (default: max mergeable)

        Returns:
            MergeResult
        """
        position = self._positions.get(market_id)
        if not position:
            return MergeResult(
                market_id=market_id,
                condition_id="",
                amount=Decimal("0"),
                success=False,
                error="Position not found"
            )

        if not position.has_both_sides:
            return MergeResult(
                market_id=market_id,
                condition_id=position.condition_id,
                amount=Decimal("0"),
                success=False,
                error="No dual-side position to merge"
            )

        merge_amount = amount or position.mergeable_amount
        if merge_amount <= 0:
            return MergeResult(
                market_id=market_id,
                condition_id=position.condition_id,
                amount=Decimal("0"),
                success=False,
                error="Nothing to merge"
            )

        condition_id = position.condition_id or self._tracked_markets.get(market_id, "")
        if not condition_id:
            return MergeResult(
                market_id=market_id,
                condition_id="",
                amount=merge_amount,
                success=False,
                error="Condition ID not found"
            )

        try:
            await self._predict.merge_positions(condition_id, merge_amount)

            result = MergeResult(
                market_id=market_id,
                condition_id=condition_id,
                amount=merge_amount,
                success=True
            )

            logger.info(
                f"Position merged",
                market_id=market_id,
                amount=str(merge_amount)
            )

            # Record and trigger callbacks
            self._merge_history.append(result)
            for callback in self._on_merge:
                try:
                    callback(result)
                except Exception as e:
                    logger.error(f"Merge callback error: {e}")

            # Refresh position after merge
            await self.refresh_positions()

            return result

        except PredictAPIError as e:
            result = MergeResult(
                market_id=market_id,
                condition_id=condition_id,
                amount=merge_amount,
                success=False,
                error=str(e)
            )
            logger.error(f"Merge failed: {e}", market_id=market_id)
            self._merge_history.append(result)
            return result

    async def auto_merge_all(self, min_amount: Decimal = Decimal("1")) -> List[MergeResult]:
        """
        Automatically merge all eligible positions.

        Args:
            min_amount: Minimum amount to merge

        Returns:
            List of MergeResults
        """
        settings = get_settings()
        if not settings.strategy.merge_enabled:
            return []

        results = []
        mergeable = self.get_mergeable_positions(min_amount)

        for position in mergeable:
            result = await self.merge_position(position.market_id)
            results.append(result)

            # Small delay between merges
            await asyncio.sleep(1)

        return results

    async def start_tracking(self) -> None:
        """Start the position tracking loop."""
        if self._running:
            return

        self._running = True
        self._poll_task = asyncio.create_task(self._track_loop())
        logger.info("Position tracking started")

    async def stop_tracking(self) -> None:
        """Stop the position tracking loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        logger.info("Position tracking stopped")

    async def _track_loop(self) -> None:
        """Main tracking loop."""
        settings = get_settings()
        interval = settings.polling.positions_interval

        while self._running:
            try:
                # Refresh positions
                await self.refresh_positions()

                # Auto-merge if enabled
                if settings.strategy.merge_enabled:
                    await self.auto_merge_all()

            except Exception as e:
                logger.error(f"Track loop error: {e}")

            await asyncio.sleep(interval)

    @property
    def is_running(self) -> bool:
        """Whether tracking is active."""
        return self._running

    def get_merge_history(self, limit: int = 50) -> List[MergeResult]:
        """Get recent merge history."""
        return self._merge_history[-limit:]

    def get_market_merges(self, market_id: str, limit: int = 10) -> List[MergeResult]:
        """Get merge history for a specific market."""
        return [
            m for m in self._merge_history
            if m.market_id == market_id
        ][-limit:]

    @property
    def total_merged(self) -> Decimal:
        """Total amount merged across all markets."""
        return sum(
            (m.amount for m in self._merge_history if m.success),
            Decimal("0")
        )
