from __future__ import annotations

from typing import Any


def sample_fio_response(transactions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "accountStatement": {
            "info": {
                "accountId": "2603445200",
                "bankId": "2010",
                "currency": "CZK",
                "iban": "CZ6508000000192000145399",
                "bic": "FIOBCZPPXXX",
                "openingBalance": "1000.00",
                "closingBalance": "1490.00",
                "dateStart": "2026-05-01+02:00",
                "dateEnd": "2026-05-16+02:00",
                "idFrom": 10,
                "idTo": 11,
            },
            "transactionList": {
                "transaction": transactions if transactions is not None else [sample_transaction()]
            },
        }
    }


def sample_transaction(
    *,
    transaction_id: str = "27573053171",
    amount: str = "490.00",
    currency: str = "CZK",
    variable_symbol: str = "2026000001",
    counterparty_name: str = "Jana Novakova",
    message: str = "Clenstvi",
) -> dict[str, Any]:
    return {
        "column22": {"value": transaction_id, "name": "ID pohybu", "id": 22},
        "column0": {"value": "2026-05-02+02:00", "name": "Datum", "id": 0},
        "column1": {"value": amount, "name": "Objem", "id": 1},
        "column14": {"value": currency, "name": "Mena", "id": 14},
        "column2": {"value": "123456789", "name": "Protiucet", "id": 2},
        "column3": {"value": "2010", "name": "Kod banky", "id": 3},
        "column5": {"value": variable_symbol, "name": "VS", "id": 5},
        "column9": {"value": "Jana Novakova", "name": "Provedl", "id": 9},
        "column10": {"value": counterparty_name, "name": "Nazev protiuctu", "id": 10},
        "column12": {"value": "Fio banka", "name": "Nazev banky", "id": 12},
        "column16": {"value": message, "name": "Zprava pro prijemce", "id": 16},
        "column25": {"value": "Internal note", "name": "Komentar", "id": 25},
        "column26": {"value": "FIOBCZPPXXX", "name": "BIC", "id": 26},
        "column27": {"value": "RF18539007547034", "name": "Reference platce", "id": 27},
    }
