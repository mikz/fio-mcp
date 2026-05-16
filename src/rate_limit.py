from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from models import RateLimitInfo, TokenPoolStatus

NowFunc = Callable[[], datetime]
SleepFunc = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class TokenLease:
    token_key: str
    rate_limit: RateLimitInfo


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
        self._selection_lock = asyncio.Lock()
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

    async def acquire_any(self, token_keys: list[str]) -> TokenLease:
        if not token_keys:
            raise ValueError("At least one token key is required")
        unique_keys = list(dict.fromkeys(token_keys))
        async with self._selection_lock:
            now = self._now()
            selected = min(
                unique_keys,
                key=lambda key: self._next_available.get(key, now),
            )
            next_available = self._next_available.get(selected, now)
            waited = 0.0
            if next_available > now:
                waited = (next_available - now).total_seconds()
                await self._sleep(waited)
                now = self._now()

            reserved_until = now + self._cooldown
            self._next_available[selected] = reserved_until
            return TokenLease(
                token_key=selected,
                rate_limit=RateLimitInfo(
                    consumed_lease=True,
                    waited_seconds=waited,
                    next_available_at=reserved_until,
                ),
            )

    def status_many(self, token_keys: list[str]) -> TokenPoolStatus:
        unique_keys = list(dict.fromkeys(token_keys))
        now = self._now()
        next_values = [self._next_available.get(key, now) for key in unique_keys]
        future = [value for value in next_values if value > now]
        return TokenPoolStatus(
            token_count=len(unique_keys),
            available_tokens=len(unique_keys) - len(future),
            next_available_at=min(future) if future else None,
        )
