# Fio MCP Design

## Goals

The server exposes a small end-user-oriented API for inspecting Fio Bank
transactions. It follows the shape of `simpleshop-mcp`: typed query objects,
explicit privacy/response controls, stable normalized models, itemized metadata,
and testable helper functions behind FastMCP tools.

## Token Handling

Reader tools accept account aliases or canonical account keys, not raw tokens.
Credentials are configured outside MCP tool calls. The stable runtime contract is
`FIO_ACCOUNTS_JSON`, which contains the full account registry. For local
development only, `FIO_TOKENS_JSON` may contain a JSON array of raw token
strings; the server validates those tokens and derives account pairing in memory
without persisting them. The legacy single-token environment variables are
intentionally not supported.

Raw tokens are never intentionally returned in tool responses, cache keys, or
error messages. `fio_list_accounts` returns stable `token_key` hash prefixes so
callers can identify token-pool state without seeing the secret value.

## Rate Limit

Fio documents a recommended minimum interval of 30 seconds for the same token.
The server reserves one local lease per token for 31 seconds. Safe `periods`
reads select an available token from the account pool, or wait for the earliest
available token if the whole pool is cooling down. A `409 Conflict` from Fio is
treated as a pacing signal for the selected token only.

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
not advance the bank-side marker. These reads can load-balance across every
token paired to the selected account.

`fio_get_new_transactions` is separate and requires explicit confirmation
because Fio's `last` endpoint advances that marker. It uses only the account's
`marker_token_key` and does not load-balance, so adding extra read tokens cannot
silently change marker semantics.
