from __future__ import annotations

import pytest

from server import mcp


@pytest.fixture
async def tool_schema() -> dict[str, dict[str, object]]:
    tools = await mcp.list_tools()
    return {
        tool.name: {
            "description": tool.description or "",
            "parameters": tool.parameters,
        }
        for tool in tools
    }


async def test_tool_set_is_stable(tool_schema: dict[str, dict[str, object]]) -> None:
    assert set(tool_schema) == {
        "fio_login",
        "fio_alias_account",
        "fio_remove_token",
        "fio_list_accounts",
        "fio_test_connection",
        "fio_find_transactions",
        "fio_get_new_transactions",
        "fio_get_metadata",
    }


async def test_every_tool_has_description(tool_schema: dict[str, dict[str, object]]) -> None:
    for name, entry in tool_schema.items():
        assert entry["description"], f"tool {name} has empty description"


def _properties(entry: dict[str, object], *path: str) -> dict[str, object]:
    node: dict[str, object] = entry["parameters"]  # type: ignore[assignment]
    for step in path:
        node = node["properties"][step]  # type: ignore[assignment,index]
    return node


async def test_find_transactions_query_fields_have_descriptions(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E1, E2: every FindTransactionsQuery field exposes a description in the JSON Schema."""
    query = _properties(tool_schema["fio_find_transactions"], "query")
    properties: dict[str, dict[str, object]] = query["properties"]  # type: ignore[assignment]

    must_have_description = [
        "account",
        "date_from",
        "date_to",
        "direction",
        "currency",
        "variable_symbol",
        "constant_symbol",
        "specific_symbol",
        "counterparty_bank_account",
        "counterparty_name",
        "counterparty_search",
        "message_search",
        "min_amount",
        "max_amount",
        "limit",
        "cursor",
        "detail_level",
        "cache",
        "max_wait_seconds",
    ]
    missing = [name for name in must_have_description if not properties.get(name, {}).get("description")]
    assert not missing, f"FindTransactionsQuery fields missing description: {missing}"


async def test_new_transactions_request_fields_have_descriptions(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    request = _properties(tool_schema["fio_get_new_transactions"], "request")
    properties: dict[str, dict[str, object]] = request["properties"]  # type: ignore[assignment]

    must_have_description = [
        "account",
        "confirm_advances_download_marker",
        "limit",
        "cursor",
        "detail_level",
        "cache",
        "max_wait_seconds",
    ]
    missing = [name for name in must_have_description if not properties.get(name, {}).get("description")]
    assert not missing, f"NewTransactionsRequest fields missing description: {missing}"


async def test_detail_level_description_warns_about_hidden_fields(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E1: detail_level description must warn that summary/counterparty hide message + user_identification."""
    query = _properties(tool_schema["fio_find_transactions"], "query")
    detail = query["properties"]["detail_level"]  # type: ignore[index]
    description = detail.get("description", "") if isinstance(detail, dict) else ""
    for keyword in ("user_identification", "message"):
        assert keyword in description, (
            f"detail_level description should mention {keyword!r} as a field hidden at lower levels"
        )


async def test_message_search_description_is_substring_aware(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E2: message_search description must say substring + case-insensitive (was bare field)."""
    query = _properties(tool_schema["fio_find_transactions"], "query")
    description = query["properties"]["message_search"].get("description", "")  # type: ignore[index]
    for keyword in ("substring", "case-insensitive"):
        assert keyword.lower() in description.lower(), (
            f"message_search description should describe {keyword!r}; got {description!r}"
        )


async def test_counterparty_search_description_is_substring_aware(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E2: counterparty_search description must say substring + case-insensitive."""
    query = _properties(tool_schema["fio_find_transactions"], "query")
    description = query["properties"]["counterparty_search"].get("description", "")  # type: ignore[index]
    for keyword in ("substring", "case-insensitive"):
        assert keyword.lower() in description.lower(), (
            f"counterparty_search description should describe {keyword!r}; got {description!r}"
        )


async def test_fio_get_metadata_returns_message_patterns_and_visibility(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """E3: fio_get_metadata is the Rosetta Stone; its docstring should hint at message_patterns
    and the detail-level visibility matrix so callers know to consult it."""
    description = tool_schema["fio_get_metadata"]["description"]
    assert isinstance(description, str)
    lowered = description.lower()
    assert "message_patterns" in lowered or "message patterns" in lowered, (
        "fio_get_metadata description should reference message_patterns"
    )


async def test_no_dead_references_to_removed_period_cap(
    tool_schema: dict[str, dict[str, object]],
) -> None:
    """T2-0 regression guard: no schema mentions period_too_large or FIO_MAX_PERIOD_DAYS."""
    blob = repr(tool_schema)
    for forbidden in ("period_too_large", "FIO_MAX_PERIOD_DAYS", "max_period_days", "suggested_date_chunks"):
        assert forbidden not in blob, (
            f"schema still references removed concept {forbidden!r}"
        )
