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


def test_counterparty_name_does_not_fall_back_to_column_9() -> None:
    """column 9 is the order initiator at our end, not the counterparty.
    When column 10 is missing, counterparty_name must be None — never
    confused with the column-9 'Provedl' field."""
    raw = sample_transaction(counterparty_name="")
    statement = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
    )
    assert statement.transactions[0].counterparty_name is None


def test_payee_hint_prefers_counterparty_name() -> None:
    raw = sample_transaction(counterparty_name="SM Production s.r.o.")
    statement = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
    )
    assert statement.transactions[0].payee_hint == "SM Production s.r.o."


def test_payee_hint_falls_back_to_z_reference_message() -> None:
    raw = sample_transaction(
        counterparty_name="",
        message="Z920260035 SM PRODUCTION S.R.O.",
    )
    statement = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
    )
    assert statement.transactions[0].payee_hint == "SM PRODUCTION S.R.O."


def test_payee_hint_falls_back_to_card_purchase_merchant() -> None:
    raw = sample_transaction(
        counterparty_name="",
        message="Nákup: Ceska posta s.p., Praha, CZ, dne 15.05.2026, castka 99.00 CZK",
    )
    statement = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
    )
    assert statement.transactions[0].payee_hint == "Ceska posta s.p."


def test_payee_hint_is_none_when_no_source_yields_a_name() -> None:
    raw = sample_transaction(counterparty_name="", message="QRPLATBA")
    statement = normalize_statement(
        sample_fio_response([raw]),
        account=sample_account(token="token"),
    )
    assert statement.transactions[0].payee_hint is None
