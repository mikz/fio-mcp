from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from models import AccountStatement, CacheInfo, CacheMode

NowFunc = Callable[[], datetime]


class ResponseCache:
    def __init__(self, *, now: NowFunc | None = None) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._now = now or (lambda: datetime.now(UTC))

    def get(self, key: str, *, mode: CacheMode) -> tuple[AccountStatement | None, CacheInfo]:
        entry = self._entries.get(key)
        if entry and entry.expires_at >= self._now():
            return entry.statement, CacheInfo(
                mode=mode,
                hit=True,
                key=key,
                cached_at=entry.cached_at,
                expires_at=entry.expires_at,
            )
        return None, CacheInfo(mode=mode, hit=False, key=key)

    def get_snapshot(self, key: str) -> AccountStatement | None:
        entry = self._entries.get(key)
        if entry and entry.expires_at >= self._now():
            return entry.statement
        return None

    def set(
        self, key: str, statement: AccountStatement, *, ttl_seconds: int, mode: CacheMode
    ) -> CacheInfo:
        now = self._now()
        entry = CacheEntry(
            statement=statement,
            cached_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        self._entries[key] = entry
        return CacheInfo(
            mode=mode,
            hit=False,
            key=key,
            cached_at=entry.cached_at,
            expires_at=entry.expires_at,
        )


class CacheEntry:
    def __init__(
        self,
        *,
        statement: AccountStatement,
        cached_at: datetime,
        expires_at: datetime,
    ) -> None:
        self.statement = statement
        self.cached_at = cached_at
        self.expires_at = expires_at
