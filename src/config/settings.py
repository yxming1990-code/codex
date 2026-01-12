"""
Configuration settings for the Predict Market Maker.
"""

from enum import Enum
from decimal import Decimal
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Environment enum for testnet/mainnet."""
    TESTNET = "testnet"
    MAINNET = "mainnet"


class PricingMode(str, Enum):
    """Pricing mode for order placement."""
    SELL_ONLY = "sell_only"      # Only place sell orders (conservative default)
    TWO_SIDED = "two_sided"      # Place both buy and sell orders


class MarketState(str, Enum):
    """State machine states for each market."""
    ACTIVE = "active"           # Running: polling prices, placing orders
    WATCH = "watch"             # Observation: no orders, waiting for manual resume
    DISABLED = "disabled"       # Disabled: cannot be auto-restored


class ExternalSource(str, Enum):
    """External price source."""
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class StrategyConfig(BaseModel):
    """Strategy configuration for a single market or global defaults."""
    pricing_mode: PricingMode = PricingMode.SELL_ONLY
    offset: Decimal = Decimal("0.01")  # Price offset from external (1 cent)
    order_size: Decimal = Decimal("10")  # Default order size in shares
    max_new_shares: int = 1000  # Maximum new shares before triggering pause
    auto_pricing_enabled: bool = True  # Whether to auto-adjust prices
    merge_enabled: bool = True  # Whether to auto-merge positions


class RiskConfig(BaseModel):
    """Risk management configuration."""
    jump_threshold: Decimal = Decimal("0.02")  # Max price jump (2 cents)
    max_consecutive_failures: int = 3  # Max consecutive external price fetch failures
    max_new_shares: int = 1000  # Max new shares per market


class RateLimitConfig(BaseModel):
    """Rate limiting configuration."""
    predict_rpm: int = 240  # Predict API requests per minute
    external_rpm: int = 120  # External API requests per minute
    retry_max_attempts: int = 4  # Max retry attempts
    retry_base_delay: float = 2.0  # Base delay for exponential backoff


class PollingConfig(BaseModel):
    """Polling interval configuration."""
    external_price_interval: int = 30  # Seconds between external price polls
    orderbook_interval: int = 30  # Seconds between Predict orderbook polls
    positions_interval: int = 60  # Seconds between position checks
    matches_interval: int = 30  # Seconds between match checks
    markets_refresh_interval: int = 300  # Seconds between market list refresh (5 min)


class PredictApiConfig(BaseModel):
    """Predict API configuration."""
    base_url_testnet: str = "https://api.predict.fun"
    base_url_mainnet: str = "https://api.predict.fun"
    timeout: int = 30  # Request timeout in seconds


class PolymarketApiConfig(BaseModel):
    """Polymarket API configuration."""
    clob_base_url: str = "https://clob.polymarket.com"
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    timeout: int = 30


class KalshiApiConfig(BaseModel):
    """Kalshi API configuration."""
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    timeout: int = 30


class Settings(BaseSettings):
    """Main application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PMM_",
        extra="ignore"
    )

    # Environment
    environment: Environment = Environment.TESTNET

    # API Keys (loaded from environment)
    predict_api_key: Optional[SecretStr] = None

    # Wallet (loaded from environment - never log or persist)
    wallet_private_key: Optional[SecretStr] = None
    wallet_address: Optional[str] = None

    # Market allowlist (comma-separated market IDs or slugs)
    market_allowlist: List[str] = Field(default_factory=list)

    # Sub-configurations
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    polling: PollingConfig = Field(default_factory=PollingConfig)
    predict_api: PredictApiConfig = Field(default_factory=PredictApiConfig)
    polymarket_api: PolymarketApiConfig = Field(default_factory=PolymarketApiConfig)
    kalshi_api: KalshiApiConfig = Field(default_factory=KalshiApiConfig)

    # Persistence
    database_path: str = "market_maker.db"

    # UI
    ui_refresh_interval: float = 1.0  # UI refresh rate in seconds

    # Safety
    use_fast_order_removal: bool = False  # POST /v1/orders/remove - risky!
    safe_exit_enabled: bool = True  # Cancel all orders on exit
    auto_resume_on_restart: bool = False  # Don't auto-resume ACTIVE markets

    @property
    def predict_base_url(self) -> str:
        """Get Predict API base URL based on environment."""
        if self.environment == Environment.TESTNET:
            return self.predict_api.base_url_testnet
        return self.predict_api.base_url_mainnet


# Global settings instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Get or create the global settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def update_settings(new_settings: Settings) -> None:
    """Update the global settings instance."""
    global _settings
    _settings = new_settings
