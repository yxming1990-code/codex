"""
Common type definitions for the Predict Market Maker.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any


class OrderSide(str, Enum):
    """Order side (buy/sell)."""
    BUY = "buy"
    SELL = "sell"


class Outcome(str, Enum):
    """Binary market outcome."""
    YES = "yes"
    NO = "no"


class OrderStatus(str, Enum):
    """Order status."""
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class PriceLevel:
    """A single price level in an order book."""
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    """Order book with bids and asks."""
    bids: List[PriceLevel] = field(default_factory=list)
    asks: List[PriceLevel] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def best_bid(self) -> Optional[Decimal]:
        """Get best bid price."""
        if self.bids:
            return max(level.price for level in self.bids)
        return None

    @property
    def best_ask(self) -> Optional[Decimal]:
        """Get best ask price."""
        if self.asks:
            return min(level.price for level in self.asks)
        return None

    @property
    def spread(self) -> Optional[Decimal]:
        """Get bid-ask spread."""
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class ExternalQuote:
    """External market quote (from Polymarket/Kalshi)."""
    source: str  # "polymarket" or "kalshi"
    market_id: str  # External market identifier
    yes_bid: Optional[Decimal] = None
    yes_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        """Check if quote has valid prices."""
        return self.yes_ask is not None or self.no_ask is not None


@dataclass
class PredictMarket:
    """Predict market information."""
    market_id: str
    condition_id: str
    title: str
    slug: str
    category: Optional[str] = None
    status: str = "unknown"
    decimal_precision: int = 2
    fee_rate_bps: int = 0

    # External market mappings
    polymarket_condition_ids: List[str] = field(default_factory=list)
    kalshi_market_ticker: Optional[str] = None

    # Token IDs for outcomes
    yes_token_id: Optional[str] = None
    no_token_id: Optional[str] = None

    # Raw data
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_external_mapping(self) -> bool:
        """Check if market has external price source mapping."""
        return bool(self.polymarket_condition_ids) or bool(self.kalshi_market_ticker)

    @property
    def external_source(self) -> Optional[str]:
        """Get the external source type."""
        if self.polymarket_condition_ids:
            return "polymarket"
        if self.kalshi_market_ticker:
            return "kalshi"
        return None


@dataclass
class PredictOrder:
    """Predict order information."""
    order_id: str
    market_id: str
    outcome: Outcome
    side: OrderSide
    price: Decimal
    size: Decimal
    filled_size: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    tx_hash: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_size(self) -> Decimal:
        """Get remaining unfilled size."""
        return self.size - self.filled_size


@dataclass
class Position:
    """Position in a Predict market."""
    market_id: str
    condition_id: str
    yes_amount: Decimal = Decimal("0")
    no_amount: Decimal = Decimal("0")
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def has_both_sides(self) -> bool:
        """Check if holding both YES and NO positions."""
        return self.yes_amount > 0 and self.no_amount > 0

    @property
    def mergeable_amount(self) -> Decimal:
        """Get the amount that can be merged."""
        return min(self.yes_amount, self.no_amount)


@dataclass
class MatchEvent:
    """Trade match event."""
    match_id: str
    order_id: str
    market_id: str
    outcome: Outcome
    side: OrderSide
    price: Decimal
    size: Decimal
    executed_at: datetime
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketStateInfo:
    """Runtime state information for a market."""
    market_id: str
    state: str  # "active", "watch", "disabled"

    # Baseline for new shares calculation
    baseline_yes_amount: Decimal = Decimal("0")
    baseline_no_amount: Decimal = Decimal("0")
    baseline_set_at: Optional[datetime] = None

    # Current tracking
    new_shares_count: Decimal = Decimal("0")
    consecutive_failures: int = 0

    # Last known external quote
    last_external_quote: Optional[ExternalQuote] = None
    last_quote_time: Optional[datetime] = None

    # Watch state info
    watch_reason: Optional[str] = None
    watch_entered_at: Optional[datetime] = None
    last_quote_snapshot: Optional[ExternalQuote] = None

    # Strategy overrides
    strategy_override: Optional[Dict[str, Any]] = None

    def calculate_new_shares(self, current_position: Position) -> Decimal:
        """Calculate new shares acquired since baseline."""
        yes_new = max(Decimal("0"), current_position.yes_amount - self.baseline_yes_amount)
        no_new = max(Decimal("0"), current_position.no_amount - self.baseline_no_amount)
        return yes_new + no_new

    def reset_baseline(self, position: Position) -> None:
        """Reset the baseline from current position."""
        self.baseline_yes_amount = position.yes_amount
        self.baseline_no_amount = position.no_amount
        self.baseline_set_at = datetime.utcnow()
        self.new_shares_count = Decimal("0")
        self.consecutive_failures = 0
