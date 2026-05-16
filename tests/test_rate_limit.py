from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rate_limit import TokenRateLimiter


async def test_same_token_waits_for_reserved_window() -> None:
    now = datetime(2026, 5, 16, tzinfo=UTC)
    sleeps: list[float] = []

    def current_time() -> datetime:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += timedelta(seconds=seconds)

    limiter = TokenRateLimiter(cooldown_seconds=31, now=current_time, sleep=sleep)

    first = await limiter.acquire("token-a")
    second = await limiter.acquire("token-a")

    assert first.waited_seconds == 0
    assert second.waited_seconds == 31
    assert sleeps == [31]


async def test_different_tokens_do_not_block_each_other() -> None:
    now = datetime(2026, 5, 16, tzinfo=UTC)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    limiter = TokenRateLimiter(cooldown_seconds=31, now=lambda: now, sleep=sleep)

    await limiter.acquire("token-a")
    second = await limiter.acquire("token-b")

    assert second.waited_seconds == 0
    assert sleeps == []
