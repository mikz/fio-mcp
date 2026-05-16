from __future__ import annotations

from decimal import Decimal

from normalization import normalize_statement
from tests.fixtures import sample_account, sample_fio_response, sample_transaction


def test_normalizes_fio_columns() -> None:
    statement = normalize_statement(
        sample_fio_response(),
        account=sample_account(token="token"),
    )

    transaction = statement.transactions[0]
    assert statement.account.alias == "main"
    assert statement.account.account == "main"
    assert statement.account.bank_account == "2603445200/2010"
    assert transaction.transaction_id == "27573053171"
    assert transaction.posted_date == "2026-05-02"
    assert transaction.amount == Decimal("490.00")
    assert transaction.direction == "incoming"
    assert transaction.variable_symbol == "2026000001"
    assert transaction.counterparty_bank_account == "123456789/2010"
    assert transaction.counterparty_name == "Jana Novakova"
    assert transaction.message == "Clenstvi"
    assert '"amount":"490.00"' in transaction.model_dump_json()


def test_normalizes_outgoing_transaction() -> None:
    statement = normalize_statement(
        sample_fio_response([sample_transaction(amount="-20.50")]),
        account=sample_account(token="token"),
    )

    assert statement.transactions[0].direction == "outgoing"
    assert statement.transactions[0].amount == Decimal("-20.50")


def test_raw_payload_is_explicit() -> None:
    raw = sample_transaction()
    without_raw = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
    )
    with_raw = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
        include_raw=True,
    )

    assert without_raw.transactions[0].raw is None
    assert with_raw.transactions[0].raw == raw
