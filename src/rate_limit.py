from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from models import RateLimitInfo

NowFunc = Callable[[], datetime]
SleepFunc = Callable[[float], Awaitable[None]]


class TokenRateLimiter:
    def __init__(
        self,
        *,
        cooldown_seconds: float,
        now: NowFunc | None = None,
        sleep: SleepFunc | None = None,
    ) -> None:
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._next_available: dict[str, datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

    async def acquire(self, token_key: str) -> RateLimitInfo:
        lock = self._locks.setdefault(token_key, asyncio.Lock())
        waited = 0.0
        async with lock:
            now = self._now()
            next_available = self._next_available.get(token_key, now)
            if next_available > now:
                waited = (next_available - now).total_seconds()
                await self._sleep(waited)
                now = self._now()

            reserved_until = now + self._cooldown
            self._next_available[token_key] = reserved_until
            return RateLimitInfo(
                consumed_lease=True,
                waited_seconds=waited,
                next_available_at=reserved_until,
            )

    def mark_conflict(self, token_key: str) -> datetime:
        next_available = self._now() + self._cooldown
        self._next_available[token_key] = next_available
        return next_available

    def status(self, token_key: str) -> RateLimitInfo:
        return RateLimitInfo(
            consumed_lease=False,
            waited_seconds=0,
            next_available_at=self._next_available.get(token_key),
        )
