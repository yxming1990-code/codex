"""
API clients module for Predict Market Maker.
"""

from .predict import (
    PredictClient,
    PredictAPIError,
)
from .polymarket import (
    PolymarketClient,
    PolymarketAPIError,
    fetch_polymarket_quote,
)
from .kalshi import (
    KalshiClient,
    KalshiAPIError,
    fetch_kalshi_quote,
)

__all__ = [
    # Predict
    "PredictClient",
    "PredictAPIError",
    # Polymarket
    "PolymarketClient",
    "PolymarketAPIError",
    "fetch_polymarket_quote",
    # Kalshi
    "KalshiClient",
    "KalshiAPIError",
    "fetch_kalshi_quote",
]
