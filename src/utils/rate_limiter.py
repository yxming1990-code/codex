"""
Rate limiting utilities for API calls.
"""

import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Callable, TypeVar, Any
from functools import wraps

from aiolimiter import AsyncLimiter

from ..config import get_settings
from .logging import get_logger

logger = get_logger(__name__)

T = TypeVar('T')


class RateLimiter:
    """
    Rate limiter with request counting and exponential backoff.
    """

    def __init__(
        self,
        requests_per_minute: int,
        name: str = "default"
    ):
        """
        Initialize rate limiter.

        Args:
            requests_per_minute: Maximum requests per minute
            name: Name for logging
        """
        self.name = name
        self.rpm = requests_per_minute
        # Allow bursting but maintain average rate
        self._limiter = AsyncLimiter(requests_per_minute, 60)
        self._request_count = 0
        self._window_start = datetime.utcnow()

    async def acquire(self) -> None:
        """Acquire a rate limit slot."""
        await self._limiter.acquire()
        self._request_count += 1

        # Reset window every minute for counting
        now = datetime.utcnow()
        if (now - self._window_start) > timedelta(minutes=1):
            self._window_start = now
            self._request_count = 1

    @property
    def current_rate(self) -> float:
        """Get current request rate per minute."""
        elapsed = (datetime.utcnow() - self._window_start).total_seconds()
        if elapsed < 1:
            return 0
        return (self._request_count / elapsed) * 60


async def retry_with_backoff(
    func: Callable[..., Any],
    *args,
    max_attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    retry_on: tuple = (Exception,),
    **kwargs
) -> Any:
    """
    Retry a function with exponential backoff.

    Args:
        func: Async function to retry
        *args: Positional arguments for func
        max_attempts: Maximum number of attempts
        base_delay: Base delay in seconds (doubles each retry)
        max_delay: Maximum delay in seconds
        jitter: Whether to add random jitter to delays
        retry_on: Exception types to retry on
        **kwargs: Keyword arguments for func

    Returns:
        Result of successful function call

    Raises:
        Last exception if all retries fail
    """
    last_exception = None

    for attempt in range(max_attempts):
        try:
            return await func(*args, **kwargs)
        except retry_on as e:
            last_exception = e
            if attempt == max_attempts - 1:
                # Last attempt, don't sleep
                break

            # Calculate delay with exponential backoff
            delay = min(base_delay * (2 ** attempt), max_delay)

            # Add jitter
            if jitter:
                delay = delay * (0.5 + random.random())

            logger.warning(
                f"Request failed, retrying",
                attempt=attempt + 1,
                max_attempts=max_attempts,
                delay=delay,
                error=str(e)
            )

            await asyncio.sleep(delay)

    raise last_exception


def rate_limited(limiter: RateLimiter):
    """
    Decorator for rate-limited async functions.

    Args:
        limiter: RateLimiter instance to use
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            await limiter.acquire()
            return await func(*args, **kwargs)
        return wrapper
    return decorator


# Global rate limiters
_predict_limiter: Optional[RateLimiter] = None
_external_limiter: Optional[RateLimiter] = None


def get_predict_limiter() -> RateLimiter:
    """Get or create the Predict API rate limiter."""
    global _predict_limiter
    if _predict_limiter is None:
        settings = get_settings()
        _predict_limiter = RateLimiter(
            settings.rate_limit.predict_rpm,
            name="predict"
        )
    return _predict_limiter


def get_external_limiter() -> RateLimiter:
    """Get or create the external API rate limiter."""
    global _external_limiter
    if _external_limiter is None:
        settings = get_settings()
        _external_limiter = RateLimiter(
            settings.rate_limit.external_rpm,
            name="external"
        )
    return _external_limiter
