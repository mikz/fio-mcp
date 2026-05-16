from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from models import AccountInfo, AccountStatement, Transaction
from settings import FioAccount


def normalize_statement(
    payload: dict[str, Any],
    *,
    account: FioAccount,
    include_raw: bool = False,
) -> AccountStatement:
    statement = payload.get("accountStatement")
    if not isinstance(statement, dict):
        raise ValueError("Fio response does not contain accountStatement")

    info = statement.get("info")
    if not isinstance(info, dict):
        raise ValueError("Fio response does not contain accountStatement.info")

    raw_transactions = statement.get("transactionList", {}).get("transaction", [])
    if raw_transactions is None:
        raw_transactions = []
    if not isinstance(raw_transactions, list):
        raise ValueError("Fio response transaction list is not an array")

    return AccountStatement(
        account=AccountInfo(
            account=account.handle,
            alias=account.alias,
            bank_account=_bank_account(
                _string_or_none(info.get("accountId")),
                _string_or_none(info.get("bankId")),
            ),
            currency=_string_or_none(info.get("currency")),
            iban=_string_or_none(info.get("iban")),
            opening_balance=_decimal_or_none(info.get("openingBalance")),
            closing_balance=_decimal_or_none(info.get("closingBalance")),
            date_start=_date_string_or_none(info.get("dateStart")),
            date_end=_date_string_or_none(info.get("dateEnd")),
            id_from=_int_or_none(info.get("idFrom")),
            id_to=_int_or_none(info.get("idTo")),
        ),
        transactions=[
            normalize_transaction(raw, include_raw=include_raw)
            for raw in raw_transactions
            if isinstance(raw, dict)
        ],
    )


def normalize_transaction(raw: dict[str, Any], *, include_raw: bool = False) -> Transaction:
    amount = _decimal_or_none(_column(raw, 1)) or Decimal("0")
    counterparty_account = _string_or_none(_column(raw, 2))
    counterparty_bank_code = _string_or_none(_column(raw, 3))
    return Transaction(
        transaction_id=_string_or_none(_column(raw, 22)) or "",
        posted_date=_date_string_or_none(_column(raw, 0)),
        amount=amount,
        currency=_string_or_none(_column(raw, 14)),
        direction="incoming" if amount >= 0 else "outgoing",
        counterparty_bank_account=_bank_account(counterparty_account, counterparty_bank_code),
        counterparty_name=_string_or_none(_column(raw, 10)) or _string_or_none(_column(raw, 9)),
        constant_symbol=_string_or_none(_column(raw, 4)),
        variable_symbol=_string_or_none(_column(raw, 5)),
        specific_symbol=_string_or_none(_column(raw, 6)),
        user_identification=_string_or_none(_column(raw, 7)),
        message=_string_or_none(_column(raw, 16)) or _string_or_none(_column(raw, 25)),
        transaction_type=_string_or_none(_column(raw, 8)),
        specification=_string_or_none(_column(raw, 18)),
        comment=_string_or_none(_column(raw, 25)),
        order_id=_string_or_none(_column(raw, 17)),
        raw=raw if include_raw else None,
    )


def _column(raw: dict[str, Any], index: int) -> Any:
    value = raw.get(f"column{index}")
    if isinstance(value, dict):
        return value.get("value")
    return None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    string = str(value).strip()
    return string or None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_string_or_none(value: Any) -> str | None:
    string = _string_or_none(value)
    if string is None:
        return None
    candidate = string.split("+", 1)[0]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return candidate


def _bank_account(account: str | None, bank_code: str | None) -> str | None:
    if not account or not bank_code:
        return None
    return f"{account}/{bank_code}"
