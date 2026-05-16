from __future__ import annotations

from datetime import date

import pytest
import respx
from httpx import Response

from client import FioClient
from server import (
    FindTransactionsQuery,
    NewTransactionsRequest,
    _find_transactions,
    _get_new_transactions,
)
from settings import Settings
from tests.fixtures import sample_fio_response, sample_transaction


def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("FIO_API_TOKEN", "token-for-test")
    monkeypatch.setenv("FIO_RATE_LIMIT_SECONDS", "0")
    monkeypatch.setenv("FIO_MAX_PERIOD_DAYS", "31")
    return Settings(_env_file=None)


def test_query_rejects_unknown_keys() -> None:
    with pytest.raises(ValueError):
        FindTransactionsQuery.model_validate(
            {
                "account": "main",
                "date_from": "2026-05-01",
                "date_to": "2026-05-16",
                "token": "must-not-be-accepted",
            }
        )


async def test_last_endpoint_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))

    result = await _get_new_transactions(
        client,
        NewTransactionsRequest(account="main", confirm_advances_download_marker=False),
    )
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "confirmation_required"


@respx.mock
async def test_find_transactions_filters_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(
        "https://fioapi.fio.cz/v1/rest/periods/token-for-test/2026-05-01/2026-05-16/transactions.json"
    ).mock(
        return_value=Response(
            200,
            json=sample_fio_response(
                [
                    sample_transaction(
                        transaction_id="1", amount="490.00", variable_symbol="2026000001"
                    ),
                    sample_transaction(
                        transaction_id="2", amount="-100.00", variable_symbol="2026000002"
                    ),
                    sample_transaction(
                        transaction_id="3", amount="990.00", variable_symbol="2026000001"
                    ),
                ]
            ),
        )
    )
    client = FioClient(settings(monkeypatch))

    first = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            direction="incoming",
            variable_symbol="2026000001",
            limit=1,
        ),
    )
    second = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 16),
            direction="incoming",
            variable_symbol="2026000001",
            limit=1,
            cursor=first.next_cursor,
        ),
    )
    await client.aclose()

    assert first.error is None
    assert first.transactions[0].transaction_id == "1"
    assert first.next_cursor is not None
    assert second.error is None
    assert second.transactions[0].transaction_id == "3"
    assert second.rate_limit.consumed_lease is False


async def test_large_period_is_rejected_without_call(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FioClient(settings(monkeypatch))

    result = await _find_transactions(
        client,
        FindTransactionsQuery(
            account="main",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 2, 15),
        ),
    )
    await client.aclose()

    assert result.error is not None
    assert result.error.code == "period_too_large"
