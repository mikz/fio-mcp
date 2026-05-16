from __future__ import annotations

import os
from datetime import date

import pytest

from client import FioClient
from server import (
    FindTransactionsQuery,
    NewTransactionsRequest,
    _find_transactions,
    _get_new_transactions,
)
from settings import Settings

pytestmark = pytest.mark.e2e


def live_settings() -> Settings:
    if os.environ.get("FIO_E2E") != "1":
        pytest.skip("Set FIO_E2E=1 to run live Fio API tests")
    return Settings()


async def test_live_period_call_and_memory_cache() -> None:
    client = FioClient(live_settings())
    today = date.today()
    query = FindTransactionsQuery(
        account=os.environ.get("FIO_E2E_ACCOUNT", "main"),
        date_from=today,
        date_to=today,
        cache="refresh",
        limit=10,
    )

    first = await _find_transactions(client, query)
    second = await _find_transactions(client, query.model_copy(update={"cache": "use"}))
    cached_only = await _find_transactions(client, query.model_copy(update={"cache": "only"}))
    await client.aclose()

    assert first.error is None
    assert first.cache.hit is False
    assert second.error is None
    assert second.cache.hit is True
    assert second.rate_limit.consumed_lease is False
    assert cached_only.error is None
    assert cached_only.cache.hit is True


async def test_live_cache_only_miss_does_not_call_fio() -> None:
    client = FioClient(live_settings())

    result = await _find_transactions(
        client,
        FindTransactionsQuery(
            account=os.environ.get("FIO_E2E_ACCOUNT", "main"),
            date_from=date(1999, 1, 1),
            date_to=date(1999, 1, 1),
            cache="only",
        ),
    )
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "cache_miss"


async def test_live_last_endpoint_requires_explicit_opt_in() -> None:
    live_settings()
    if os.environ.get("FIO_E2E_ALLOW_LAST") != "1":
        pytest.skip("Set FIO_E2E_ALLOW_LAST=1 to run the marker-advancing live test")

    client = FioClient(Settings())
    result = await _get_new_transactions(
        client,
        NewTransactionsRequest(
            account=os.environ.get("FIO_E2E_ACCOUNT", "main"),
            confirm_advances_download_marker=True,
            cache="refresh",
        ),
    )
    await client.aclose()

    assert result.error is None
