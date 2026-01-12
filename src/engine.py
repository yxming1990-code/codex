"""
Main market maker engine.

Integrates all components:
- Market discovery and mapping
- Price feed polling
- Order management
- Risk management
- Position tracking
- Persistence
- UI updates
"""

import asyncio
import signal
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, List, Set, Callable

from .config import get_settings, MarketState, PricingMode
from .api import PredictClient, PolymarketClient, KalshiClient
from .market import MarketDiscovery, MarketMapping
from .price import PriceFeedEngine
from .order import OrderManager
from .risk import RiskManager, RiskAlert
from .position import PositionTracker
from .persistence import Database
from .ui import MarketMakerApp, MarketRow
from .utils import (
    get_logger,
    setup_logging,
    ExternalQuote,
    PredictMarket,
    Position,
)

logger = get_logger(__name__)


class MarketMakerEngine:
    """
    Main market maker engine coordinating all components.
    """

    def __init__(self):
        """Initialize engine."""
        self._settings = get_settings()

        # API clients
        self._predict: Optional[PredictClient] = None
        self._polymarket: Optional[PolymarketClient] = None
        self._kalshi: Optional[KalshiClient] = None

        # Core components
        self._discovery: Optional[MarketDiscovery] = None
        self._price_feed: Optional[PriceFeedEngine] = None
        self._order_manager: Optional[OrderManager] = None
        self._risk_manager: Optional[RiskManager] = None
        self._position_tracker: Optional[PositionTracker] = None
        self._database: Optional[Database] = None

        # UI
        self._app: Optional[MarketMakerApp] = None

        # State
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._main_loop_task: Optional[asyncio.Task] = None

        # Market data
        self._markets: Dict[str, PredictMarket] = {}
        self._mappings: Dict[str, MarketMapping] = {}

    async def initialize(self) -> None:
        """Initialize all components."""
        logger.info("Initializing market maker engine...")

        # Initialize API clients
        self._predict = PredictClient()
        self._polymarket = PolymarketClient()
        self._kalshi = KalshiClient()

        # Enter async contexts
        await self._predict.__aenter__()
        await self._polymarket.__aenter__()
        await self._kalshi.__aenter__()

        # Initialize database
        self._database = Database()
        await self._database.connect()

        # Initialize components
        self._discovery = MarketDiscovery(
            self._predict, self._polymarket, self._kalshi
        )
        self._price_feed = PriceFeedEngine(self._polymarket, self._kalshi)
        self._order_manager = OrderManager(self._predict)
        self._risk_manager = RiskManager(self._predict)
        self._position_tracker = PositionTracker(self._predict, self._discovery)

        # Register callbacks
        self._setup_callbacks()

        # Load persisted state
        await self._load_state()

        logger.info("Engine initialized")

    async def shutdown(self) -> None:
        """Shutdown all components."""
        logger.info("Shutting down market maker engine...")

        self._running = False
        self._shutdown_event.set()

        # Cancel main loop
        if self._main_loop_task:
            self._main_loop_task.cancel()
            try:
                await self._main_loop_task
            except asyncio.CancelledError:
                pass

        # Stop background tasks
        if self._price_feed:
            await self._price_feed.stop_polling()
        if self._position_tracker:
            await self._position_tracker.stop_tracking()

        # Safe exit: cancel all orders if enabled
        if self._settings.safe_exit_enabled and self._order_manager:
            logger.info("Safe exit: cancelling all orders...")
            try:
                cancelled = await self._order_manager.cancel_all_orders()
                logger.info(f"Cancelled {cancelled} orders on exit")
            except Exception as e:
                logger.error(f"Failed to cancel orders on exit: {e}")

        # Save state
        await self._save_state()

        # Close database
        if self._database:
            await self._database.close()

        # Close API clients
        if self._predict:
            await self._predict.__aexit__(None, None, None)
        if self._polymarket:
            await self._polymarket.__aexit__(None, None, None)
        if self._kalshi:
            await self._kalshi.__aexit__(None, None, None)

        logger.info("Engine shutdown complete")

    def _setup_callbacks(self) -> None:
        """Set up component callbacks."""
        # Price feed callbacks
        self._price_feed.on_price_update(self._on_price_update)
        self._price_feed.on_price_jump(self._on_price_jump)
        self._price_feed.on_fetch_failure(self._on_fetch_failure)

        # Risk manager callbacks
        self._risk_manager.on_state_change(self._on_state_change)
        self._risk_manager.on_risk_alert(self._on_risk_alert)

        # Position tracker callbacks
        self._position_tracker.on_position_update(self._on_position_update)
        self._position_tracker.on_merge(self._on_merge)

    def _on_price_update(self, market_id: str, quote: ExternalQuote) -> None:
        """Handle price update from feed."""
        # Update risk manager
        self._risk_manager.update_quote(market_id, quote)

        # Trigger rebalance if active
        if self._risk_manager.is_active(market_id):
            asyncio.create_task(self._rebalance_market(market_id, quote))

        # Update UI
        self._update_ui_market(market_id)

    def _on_price_jump(self, market_id: str, reason: str) -> None:
        """Handle price jump detection."""
        self._risk_manager.handle_price_jump(market_id, reason)

    def _on_fetch_failure(self, market_id: str, failures: int, error: str) -> None:
        """Handle fetch failure."""
        self._risk_manager.handle_fetch_failure(market_id, failures, error)

    def _on_state_change(
        self,
        market_id: str,
        old_state: MarketState,
        new_state: MarketState,
        reason: Optional[str]
    ) -> None:
        """Handle market state change."""
        logger.info(
            f"State change: {old_state.value} -> {new_state.value}",
            market_id=market_id,
            reason=reason
        )

        # Cancel orders when leaving ACTIVE
        if old_state == MarketState.ACTIVE and new_state != MarketState.ACTIVE:
            asyncio.create_task(self._order_manager.cancel_all_orders(market_id))

        # Persist state change
        asyncio.create_task(self._persist_state_change(market_id, new_state, reason))

        # Update UI
        self._update_ui_market(market_id)
        if self._app:
            self._app.add_alert(market_id, new_state.value, reason or "State change")

    def _on_risk_alert(self, alert: RiskAlert) -> None:
        """Handle risk alert."""
        # Persist alert
        asyncio.create_task(
            self._database.save_alert(
                alert.market_id,
                alert.event.value,
                alert.reason,
                alert.data
            )
        )

        # Update UI
        if self._app:
            self._app.add_alert(alert.market_id, alert.event.value, alert.reason)

    def _on_position_update(self, market_id: str, position: Position) -> None:
        """Handle position update."""
        # Check new shares threshold
        asyncio.create_task(self._risk_manager.check_new_shares(market_id))

        # Update UI
        self._update_ui_market(market_id)

    def _on_merge(self, result) -> None:
        """Handle merge result."""
        logger.info(
            f"Merge {'succeeded' if result.success else 'failed'}",
            market_id=result.market_id,
            amount=str(result.amount)
        )

        # Persist operation
        asyncio.create_task(
            self._database.log_operation(
                "merge",
                result.market_id,
                {"amount": str(result.amount)},
                result.success,
                result.error
            )
        )

    async def _rebalance_market(self, market_id: str, quote: ExternalQuote) -> None:
        """Rebalance orders for a market."""
        market = self._markets.get(market_id)
        if not market:
            return

        try:
            result = await self._order_manager.rebalance(market, quote)

            # Log operation
            await self._database.log_operation(
                "rebalance",
                market_id,
                {
                    "cancelled": result["cancelled"],
                    "placed": result["placed"],
                    "kept": result["kept"],
                },
                not result["errors"],
                "; ".join(result["errors"]) if result["errors"] else None
            )

        except Exception as e:
            logger.error(f"Rebalance failed: {e}", market_id=market_id)

    async def _persist_state_change(
        self,
        market_id: str,
        new_state: MarketState,
        reason: Optional[str]
    ) -> None:
        """Persist market state change."""
        await self._database.update_market_state(market_id, new_state, reason)

    async def _load_state(self) -> None:
        """Load persisted state."""
        # Load markets from database
        db_markets = await self._database.get_all_markets()

        for db_market in db_markets:
            market_id = db_market["market_id"]

            # Register with risk manager
            state = MarketState(db_market.get("state", "watch"))
            self._risk_manager.register_market(market_id, state)

            # Set baseline if available
            state_info = self._risk_manager.get_state(market_id)
            if state_info and db_market.get("baseline_set_at"):
                state_info.baseline_yes_amount = Decimal(
                    db_market.get("baseline_yes_amount", "0")
                )
                state_info.baseline_no_amount = Decimal(
                    db_market.get("baseline_no_amount", "0")
                )
                state_info.new_shares_count = Decimal(
                    db_market.get("new_shares_count", "0")
                )

            # Load strategy overrides
            override = await self._database.get_strategy_override(market_id)
            if override:
                self._order_manager.set_strategy_override(market_id, **override)

        logger.info(f"Loaded {len(db_markets)} markets from database")

    async def _save_state(self) -> None:
        """Save current state to database."""
        for market_id, state_info in self._risk_manager._states.items():
            await self._database.update_market_baseline(
                market_id,
                state_info.baseline_yes_amount,
                state_info.baseline_no_amount
            )
            await self._database.update_new_shares(
                market_id,
                state_info.new_shares_count
            )

    def _update_ui_market(self, market_id: str) -> None:
        """Update UI for a market."""
        if not self._app:
            return

        market = self._markets.get(market_id)
        if not market:
            return

        state_info = self._risk_manager.get_state(market_id)
        quote = self._price_feed.get_quote(market_id) if self._price_feed else None
        position = self._position_tracker.get_position(market_id) if self._position_tracker else None
        market_orders = self._order_manager.get_market_orders(market_id) if self._order_manager else None

        row = MarketRow(
            market_id=market_id,
            title=market.title,
            state=MarketState(state_info.state) if state_info else MarketState.DISABLED,
            external_source=market.external_source or "",
            yes_bid=quote.yes_bid if quote else None,
            yes_ask=quote.yes_ask if quote else None,
            no_bid=quote.no_bid if quote else None,
            no_ask=quote.no_ask if quote else None,
            new_shares=state_info.new_shares_count if state_info else Decimal("0"),
            max_shares=self._settings.risk.max_new_shares,
            open_orders=len(market_orders.get_open_orders()) if market_orders else 0,
            yes_position=position.yes_amount if position else Decimal("0"),
            no_position=position.no_amount if position else Decimal("0"),
        )

        self._app.update_market(row)

    def _update_ui_status(self) -> None:
        """Update UI status bar."""
        if not self._app:
            return

        active = len(self._risk_manager.get_active_markets())
        watch = len(self._risk_manager.get_watch_markets())
        orders = self._order_manager.total_open_orders if self._order_manager else 0

        self._app.update_status(active, watch, orders, self._running)

    # ========== Public API ==========

    async def discover_markets(self) -> List[MarketMapping]:
        """Discover and validate markets."""
        logger.info("Discovering markets...")

        # Refresh markets
        await self._discovery.refresh_markets()

        # Get tradable markets
        tradable = self._discovery.get_tradable_markets()

        # Create mappings
        for market in tradable:
            mapping = self._discovery.create_mapping(market)
            if mapping:
                self._markets[market.market_id] = market
                self._mappings[market.market_id] = mapping

        # Validate mappings
        await self._discovery.validate_all_mappings()

        # Get validated mappings
        validated = self._discovery.get_validated_mappings()

        logger.info(f"Discovered {len(validated)} validated markets")

        return validated

    async def select_market(self, market_id: str) -> bool:
        """
        Select a market for trading.

        Args:
            market_id: Market ID to select

        Returns:
            True if selection successful
        """
        market = self._markets.get(market_id)
        mapping = self._mappings.get(market_id)

        if not market or not mapping:
            logger.warning(f"Market not found: {market_id}")
            return False

        if not mapping.validated:
            logger.warning(f"Market mapping not validated: {market_id}")
            return False

        # Register with components
        self._risk_manager.register_market(market_id, MarketState.WATCH)
        self._order_manager.register_market(market_id)
        self._position_tracker.register_market(market_id, market.condition_id)
        self._price_feed.add_feed(mapping)

        # Persist to database
        await self._database.save_market(
            market_id=market_id,
            condition_id=market.condition_id,
            title=market.title,
            external_source=mapping.external_source.value,
            external_id=mapping.external_id,
            state=MarketState.WATCH
        )

        logger.info(f"Selected market: {market_id}")
        return True

    async def activate_market(self, market_id: str) -> bool:
        """Activate a market for trading."""
        # Get current position for baseline
        position = await self._predict.get_position(market_id)

        # Activate in risk manager
        success = await self._risk_manager.activate_market(market_id, position)

        if success:
            # Update baseline in database
            state_info = self._risk_manager.get_state(market_id)
            if state_info:
                await self._database.update_market_baseline(
                    market_id,
                    state_info.baseline_yes_amount,
                    state_info.baseline_no_amount
                )

        return success

    def pause_market(self, market_id: str, reason: str = "Manual pause") -> bool:
        """Pause a market."""
        return self._risk_manager.pause_market(market_id, reason)

    async def cancel_market_orders(self, market_id: str) -> int:
        """Cancel all orders for a market."""
        return await self._order_manager.cancel_all_orders(market_id)

    async def cancel_all_orders(self) -> int:
        """Cancel all open orders."""
        return await self._order_manager.cancel_all_orders()

    async def start(self) -> None:
        """Start the market maker."""
        if self._running:
            return

        self._running = True
        logger.info("Starting market maker...")

        # Start background tasks
        await self._price_feed.start_polling()
        await self._position_tracker.start_tracking()

        # Activate markets that were active before (if auto-resume enabled)
        if self._settings.auto_resume_on_restart:
            active_markets = await self._database.get_markets_by_state(MarketState.ACTIVE)
            for db_market in active_markets:
                await self.activate_market(db_market["market_id"])

        # Start main loop
        self._main_loop_task = asyncio.create_task(self._main_loop())

        logger.info("Market maker started")

    async def stop(self) -> None:
        """Stop the market maker."""
        if not self._running:
            return

        logger.info("Stopping market maker...")

        self._running = False

        # Pause all markets
        for market_id in self._risk_manager.get_active_markets():
            self._risk_manager.pause_market(market_id, "System stop")

        # Cancel all orders
        await self.cancel_all_orders()

        # Stop background tasks
        await self._price_feed.stop_polling()
        await self._position_tracker.stop_tracking()

        # Cancel main loop
        if self._main_loop_task:
            self._main_loop_task.cancel()
            try:
                await self._main_loop_task
            except asyncio.CancelledError:
                pass

        logger.info("Market maker stopped")

    async def _main_loop(self) -> None:
        """Main processing loop."""
        while self._running:
            try:
                # Update UI
                self._update_ui_status()
                for market_id in self._markets:
                    self._update_ui_market(market_id)

                # Periodic tasks
                await asyncio.sleep(1)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}")
                await asyncio.sleep(5)

    def handle_action(self, action: str, market_id: Optional[str] = None) -> None:
        """
        Handle UI action.

        Args:
            action: Action name
            market_id: Optional market ID
        """
        logger.info(f"Action: {action}", market_id=market_id)

        if action == "start_all":
            asyncio.create_task(self.start())
        elif action == "pause_all":
            asyncio.create_task(self.stop())
        elif action == "cancel_all":
            asyncio.create_task(self.cancel_all_orders())
        elif action == "exit":
            self._shutdown_event.set()
        elif action == "activate" and market_id:
            asyncio.create_task(self.activate_market(market_id))
        elif action == "pause" and market_id:
            self.pause_market(market_id)
        elif action == "cancel" and market_id:
            asyncio.create_task(self.cancel_market_orders(market_id))
        elif action == "refresh":
            asyncio.create_task(self.discover_markets())

    async def run_with_ui(self) -> None:
        """Run engine with TUI."""
        # Create app with action handler
        self._app = MarketMakerApp(on_action=self.handle_action)

        # Initialize engine
        await self.initialize()

        # Discover markets
        await self.discover_markets()

        # Select markets from allowlist
        for market_id in self._settings.market_allowlist:
            if market_id in self._markets:
                await self.select_market(market_id)

        # Run app and engine together
        try:
            async with asyncio.TaskGroup() as tg:
                # Run UI
                tg.create_task(self._app.run_async())

                # Wait for shutdown
                tg.create_task(self._wait_for_shutdown())

        except* asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def _wait_for_shutdown(self) -> None:
        """Wait for shutdown signal."""
        await self._shutdown_event.wait()
        self._app.exit()

    async def run_headless(self) -> None:
        """Run engine without UI (headless mode)."""
        # Initialize
        await self.initialize()

        # Discover markets
        await self.discover_markets()

        # Select markets from allowlist
        for market_id in self._settings.market_allowlist:
            if market_id in self._markets:
                await self.select_market(market_id)
                await self.activate_market(market_id)

        # Start
        await self.start()

        # Wait for shutdown signal
        try:
            await self._shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()
