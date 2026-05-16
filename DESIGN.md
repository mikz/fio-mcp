# Fio MCP Design

## Goals

The server exposes a small end-user-oriented API for inspecting Fio Bank
transactions. It follows the shape of `simpleshop-mcp`: typed query objects,
explicit privacy/response controls, stable normalized models, itemized metadata,
and testable helper functions behind FastMCP tools.

## Token Handling

Tools accept account aliases, not raw tokens. Tokens are configured through
environment variables and are never intentionally returned in tool responses,
cache keys, or error messages.

## Rate Limit

Fio documents a recommended minimum interval of 30 seconds for the same token.
The server reserves one local lease per token for 31 seconds. A `409 Conflict`
from Fio is treated as a pacing signal.

This guard is process-local. If two separate MCP server processes share one Fio
token, they can still collide at the bank.

## Cache

Agents often retry the same call, ask for a cursor page, or change local filters.
The server therefore caches successful Fio responses in memory and applies
filters/cursors locally.

Completed historical periods are cached longer than active periods because they
are effectively immutable for normal accounting review. Cursor pagination is
bound to the cached snapshot; expired cursors return `cursor_expired` instead of
silently consuming another token lease.

## `periods` vs `last`

`fio_find_transactions` uses the `periods` endpoint by default because it does
not advance the bank-side marker. `fio_get_new_transactions` is separate and
requires explicit confirmation because Fio's `last` endpoint advances that
marker.
