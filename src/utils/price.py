"""
Price utilities for handling decimal precision and conversions.
"""

from decimal import Decimal, ROUND_DOWN, ROUND_UP, InvalidOperation
from typing import Optional


def round_price(
    price: Decimal,
    decimal_precision: int,
    rounding: str = ROUND_DOWN
) -> Decimal:
    """
    Round a price to the specified decimal precision.

    Args:
        price: Price to round
        decimal_precision: Number of decimal places
        rounding: Rounding mode (ROUND_DOWN, ROUND_UP, etc.)

    Returns:
        Rounded price
    """
    quantize_str = "0." + "0" * decimal_precision
    return price.quantize(Decimal(quantize_str), rounding=rounding)


def complement_price(price: Decimal, decimal_precision: int = 2) -> Decimal:
    """
    Calculate the complement price (1 - price) for binary markets.
    YES + NO = 1 in binary prediction markets.

    Args:
        price: Original price (0 < price < 1)
        decimal_precision: Precision for rounding

    Returns:
        Complement price
    """
    complement = Decimal("1") - price
    return round_price(complement, decimal_precision)


def kalshi_cents_to_decimal(cents: int) -> Decimal:
    """
    Convert Kalshi cents (1-99) to decimal (0.01-0.99).

    Args:
        cents: Price in cents (1-99)

    Returns:
        Decimal price (0.01-0.99)
    """
    return Decimal(cents) / Decimal("100")


def decimal_to_kalshi_cents(price: Decimal) -> int:
    """
    Convert decimal price to Kalshi cents.

    Args:
        price: Decimal price (0.01-0.99)

    Returns:
        Price in cents (1-99)
    """
    return int(price * 100)


def validate_price(price: Decimal) -> bool:
    """
    Validate that a price is in valid range (0, 1).

    Args:
        price: Price to validate

    Returns:
        True if valid
    """
    return Decimal("0") < price < Decimal("1")


def safe_decimal(value: any, default: Decimal = Decimal("0")) -> Decimal:
    """
    Safely convert a value to Decimal.

    Args:
        value: Value to convert
        default: Default value if conversion fails

    Returns:
        Decimal value
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def calculate_target_price(
    external_price: Decimal,
    offset: Decimal,
    is_sell: bool,
    decimal_precision: int
) -> Decimal:
    """
    Calculate target order price based on external reference.

    For sell orders: we want to be slightly below external ask (more attractive)
    For buy orders: we want to be slightly below external bid (more conservative)

    Args:
        external_price: External market price
        offset: Price offset (positive value)
        is_sell: True for sell orders, False for buy orders
        decimal_precision: Market's decimal precision

    Returns:
        Target order price
    """
    if is_sell:
        # Sell: match or slightly undercut external ask
        target = external_price - offset
    else:
        # Buy: bid slightly below external bid (conservative)
        target = external_price - offset

    # Ensure price is valid
    target = max(Decimal("0.01"), min(Decimal("0.99"), target))

    return round_price(target, decimal_precision)


def price_change_pct(old_price: Decimal, new_price: Decimal) -> Decimal:
    """
    Calculate percentage price change.

    Args:
        old_price: Previous price
        new_price: New price

    Returns:
        Absolute percentage change
    """
    if old_price == 0:
        return Decimal("100") if new_price != 0 else Decimal("0")
    return abs((new_price - old_price) / old_price) * 100


def price_jump_exceeded(
    old_price: Decimal,
    new_price: Decimal,
    threshold: Decimal
) -> bool:
    """
    Check if price jump exceeds threshold.

    Args:
        old_price: Previous price
        new_price: New price
        threshold: Jump threshold (e.g., 0.02 for 2 cents)

    Returns:
        True if jump exceeds threshold
    """
    return abs(new_price - old_price) > threshold
