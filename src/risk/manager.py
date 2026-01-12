"""
Risk manager with market state machine.

Handles:
- Market state transitions (ACTIVE, WATCH, DISABLED)
- Risk event detection and response
- New shares tracking
- Automatic pause triggers
"""

import asyncio
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, List, Callable, Any
from dataclasses import dataclass, field
from enum import Enum

from ..config import get_settings, MarketState
from ..api import PredictClient
from ..utils import (
    get_logger,
    Position,
    ExternalQuote,
    MarketStateInfo,
)

logger = get_logger(__name__)


class RiskEvent(Enum):
    """Types of risk events."""
    PRICE_JUMP = "price_jump"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    NEW_SHARES_THRESHOLD = "new_shares_threshold"
    MARKET_CLOSED = "market_closed"
    MANUAL_PAUSE = "manual_pause"
    MANUAL_DISABLE = "manual_disable"


@dataclass
class RiskAlert:
    """A risk alert event."""
    market_id: str
    event: RiskEvent
    reason: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    data: Dict[str, Any] = field(default_factory=dict)


# Callback types
StateChangeCallback = Callable[[str, MarketState, MarketState, Optional[str]], None]
RiskAlertCallback = Callable[[RiskAlert], None]


class RiskManager:
    """
    Risk manager with market state machine.

    Manages market states and triggers risk responses.

    States:
    - ACTIVE: Running, polling prices, placing orders
    - WATCH: Paused, no orders, waiting for manual resume
    - DISABLED: Cannot be auto-restored

    Transitions:
    - ACTIVE -> WATCH: Risk event triggered
    - WATCH -> ACTIVE: Manual resume
    - WATCH -> DISABLED: Manual disable or market closed
    - ACTIVE -> DISABLED: Market closed
    """

    def __init__(self, predict_client: PredictClient):
        """
        Initialize risk manager.

        Args:
            predict_client: Predict API client for position fetching
        """
        self._predict = predict_client

        # State per market
        self._states: Dict[str, MarketStateInfo] = {}

        # Callbacks
        self._on_state_change: List[StateChangeCallback] = []
        self._on_risk_alert: List[RiskAlertCallback] = []

        # Alert history
        self._alerts: List[RiskAlert] = []
        self._max_alerts = 1000

    def register_market(self, market_id: str, initial_state: MarketState = MarketState.WATCH) -> None:
        """
        Register a market for risk management.

        Args:
            market_id: Market ID to register
            initial_state: Initial state (default WATCH for safety)
        """
        if market_id not in self._states:
            self._states[market_id] = MarketStateInfo(
                market_id=market_id,
                state=initial_state.value
            )
            logger.info(f"Registered market {market_id} with state {initial_state.value}")

    def unregister_market(self, market_id: str) -> None:
        """Unregister a market."""
        if market_id in self._states:
            del self._states[market_id]

    def get_state(self, market_id: str) -> Optional[MarketStateInfo]:
        """Get state info for a market."""
        return self._states.get(market_id)

    def get_market_state(self, market_id: str) -> MarketState:
        """Get current state enum for a market."""
        state_info = self._states.get(market_id)
        if state_info:
            return MarketState(state_info.state)
        return MarketState.DISABLED

    def is_active(self, market_id: str) -> bool:
        """Check if a market is in ACTIVE state."""
        return self.get_market_state(market_id) == MarketState.ACTIVE

    def is_watch(self, market_id: str) -> bool:
        """Check if a market is in WATCH state."""
        return self.get_market_state(market_id) == MarketState.WATCH

    # Callback registration
    def on_state_change(self, callback: StateChangeCallback) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)

    def on_risk_alert(self, callback: RiskAlertCallback) -> None:
        """Register callback for risk alerts."""
        self._on_risk_alert.append(callback)

    def _transition(
        self,
        market_id: str,
        new_state: MarketState,
        reason: Optional[str] = None
    ) -> bool:
        """
        Perform a state transition.

        Args:
            market_id: Market ID
            new_state: Target state
            reason: Optional reason for transition

        Returns:
            True if transition occurred
        """
        state_info = self._states.get(market_id)
        if not state_info:
            return False

        old_state = MarketState(state_info.state)

        if old_state == new_state:
            return False

        # Update state
        state_info.state = new_state.value

        # Set watch info if entering WATCH
        if new_state == MarketState.WATCH:
            state_info.watch_reason = reason
            state_info.watch_entered_at = datetime.utcnow()
            state_info.last_quote_snapshot = state_info.last_external_quote

        # Clear watch info if leaving WATCH
        if old_state == MarketState.WATCH:
            # Keep the reason for historical reference

            pass

        logger.info(
            f"State transition: {old_state.value} -> {new_state.value}",
            market_id=market_id,
            reason=reason
        )

        # Trigger callbacks
        for callback in self._on_state_change:
            try:
                callback(market_id, old_state, new_state, reason)
            except Exception as e:
                logger.error(f"State change callback error: {e}")

        return True

    def _record_alert(self, alert: RiskAlert) -> None:
        """Record a risk alert."""
        self._alerts.append(alert)

        # Trim old alerts
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]

        # Trigger callbacks
        for callback in self._on_risk_alert:
            try:
                callback(alert)
            except Exception as e:
                logger.error(f"Risk alert callback error: {e}")

    # ========== Risk Event Handlers ==========

    def handle_price_jump(self, market_id: str, reason: str) -> bool:
        """
        Handle a price jump event.

        Args:
            market_id: Market ID
            reason: Description of the jump

        Returns:
            True if state changed
        """
        if not self.is_active(market_id):
            return False

        alert = RiskAlert(
            market_id=market_id,
            event=RiskEvent.PRICE_JUMP,
            reason=reason
        )
        self._record_alert(alert)

        return self._transition(
            market_id,
            MarketState.WATCH,
            f"Price jump: {reason}"
        )

    def handle_fetch_failure(self, market_id: str, failures: int, error: str) -> bool:
        """
        Handle consecutive fetch failures.

        Args:
            market_id: Market ID
            failures: Number of consecutive failures
            error: Last error message

        Returns:
            True if state changed (when threshold reached)
        """
        settings = get_settings()
        state_info = self._states.get(market_id)
        if not state_info:
            return False

        state_info.consecutive_failures = failures

        if failures >= settings.risk.max_consecutive_failures:
            if not self.is_active(market_id):
                return False

            alert = RiskAlert(
                market_id=market_id,
                event=RiskEvent.CONSECUTIVE_FAILURES,
                reason=f"Failed {failures} times: {error}",
                data={"failures": failures, "error": error}
            )
            self._record_alert(alert)

            return self._transition(
                market_id,
                MarketState.WATCH,
                f"Consecutive failures ({failures}): {error}"
            )

        return False

    def handle_new_shares_threshold(
        self,
        market_id: str,
        new_shares: Decimal,
        threshold: Decimal
    ) -> bool:
        """
        Handle new shares threshold breach.

        Args:
            market_id: Market ID
            new_shares: Current new shares count
            threshold: Threshold that was breached

        Returns:
            True if state changed
        """
        if not self.is_active(market_id):
            return False

        alert = RiskAlert(
            market_id=market_id,
            event=RiskEvent.NEW_SHARES_THRESHOLD,
            reason=f"New shares {new_shares} >= threshold {threshold}",
            data={"new_shares": str(new_shares), "threshold": str(threshold)}
        )
        self._record_alert(alert)

        return self._transition(
            market_id,
            MarketState.WATCH,
            f"New shares threshold: {new_shares} >= {threshold}"
        )

    def handle_market_closed(self, market_id: str) -> bool:
        """
        Handle market closure.

        Args:
            market_id: Market ID

        Returns:
            True if state changed
        """
        alert = RiskAlert(
            market_id=market_id,
            event=RiskEvent.MARKET_CLOSED,
            reason="Market closed or not tradable"
        )
        self._record_alert(alert)

        return self._transition(
            market_id,
            MarketState.DISABLED,
            "Market closed"
        )

    # ========== Manual Controls ==========

    async def activate_market(
        self,
        market_id: str,
        position: Optional[Position] = None
    ) -> bool:
        """
        Manually activate a market from WATCH state.

        Args:
            market_id: Market ID
            position: Optional current position for baseline reset

        Returns:
            True if activation successful
        """
        state_info = self._states.get(market_id)
        if not state_info:
            return False

        if state_info.state == MarketState.DISABLED.value:
            logger.warning(f"Cannot activate disabled market {market_id}")
            return False

        # Reset baseline if position provided
        if position:
            state_info.reset_baseline(position)
        else:
            # Fetch position
            try:
                pos = await self._predict.get_position(market_id)
                if pos:
                    state_info.reset_baseline(pos)
            except Exception as e:
                logger.error(f"Failed to fetch position for baseline: {e}")

        # Reset failure counter
        state_info.consecutive_failures = 0

        return self._transition(
            market_id,
            MarketState.ACTIVE,
            "Manual activation"
        )

    def pause_market(self, market_id: str, reason: str = "Manual pause") -> bool:
        """
        Manually pause a market to WATCH state.

        Args:
            market_id: Market ID
            reason: Reason for pausing

        Returns:
            True if state changed
        """
        alert = RiskAlert(
            market_id=market_id,
            event=RiskEvent.MANUAL_PAUSE,
            reason=reason
        )
        self._record_alert(alert)

        return self._transition(market_id, MarketState.WATCH, reason)

    def disable_market(self, market_id: str, reason: str = "Manual disable") -> bool:
        """
        Manually disable a market.

        Args:
            market_id: Market ID
            reason: Reason for disabling

        Returns:
            True if state changed
        """
        alert = RiskAlert(
            market_id=market_id,
            event=RiskEvent.MANUAL_DISABLE,
            reason=reason
        )
        self._record_alert(alert)

        return self._transition(market_id, MarketState.DISABLED, reason)

    # ========== Position Tracking ==========

    def update_quote(self, market_id: str, quote: ExternalQuote) -> None:
        """Update last known quote for a market."""
        state_info = self._states.get(market_id)
        if state_info:
            state_info.last_external_quote = quote
            state_info.last_quote_time = datetime.utcnow()

    async def check_new_shares(self, market_id: str) -> Optional[Decimal]:
        """
        Check new shares for a market against threshold.

        Args:
            market_id: Market ID

        Returns:
            New shares count, or None if not trackable
        """
        state_info = self._states.get(market_id)
        if not state_info or state_info.baseline_set_at is None:
            return None

        try:
            position = await self._predict.get_position(market_id)
            if not position:
                return Decimal("0")

            new_shares = state_info.calculate_new_shares(position)
            state_info.new_shares_count = new_shares

            # Check threshold
            settings = get_settings()
            max_shares = Decimal(str(settings.risk.max_new_shares))

            if new_shares >= max_shares:
                self.handle_new_shares_threshold(market_id, new_shares, max_shares)

            return new_shares

        except Exception as e:
            logger.error(f"Failed to check new shares: {e}", market_id=market_id)
            return None

    # ========== Queries ==========

    def get_active_markets(self) -> List[str]:
        """Get list of active market IDs."""
        return [
            mid for mid, state in self._states.items()
            if state.state == MarketState.ACTIVE.value
        ]

    def get_watch_markets(self) -> List[str]:
        """Get list of markets in watch state."""
        return [
            mid for mid, state in self._states.items()
            if state.state == MarketState.WATCH.value
        ]

    def get_disabled_markets(self) -> List[str]:
        """Get list of disabled market IDs."""
        return [
            mid for mid, state in self._states.items()
            if state.state == MarketState.DISABLED.value
        ]

    def get_recent_alerts(self, limit: int = 50) -> List[RiskAlert]:
        """Get recent risk alerts."""
        return self._alerts[-limit:]

    def get_market_alerts(self, market_id: str, limit: int = 10) -> List[RiskAlert]:
        """Get alerts for a specific market."""
        return [
            a for a in self._alerts
            if a.market_id == market_id
        ][-limit:]
