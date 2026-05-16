# Fio MCP

Read-only [FastMCP](https://gofastmcp.com/) server for Fio Bank transaction data.

The server is designed for accounting and operations agents that need to inspect
bank movements without mutating bank state. It uses Fio's read APIs only.

## Features

- Multiple configured Fio API tokens, addressed by account alias.
- No raw token tool parameters, responses, cache keys, or intentional logs.
- Per-token pacing with a 31 second local lease for Fio's 30 second guidance.
- Explicit cache control: `use`, `refresh`, or `only`.
- Longer in-memory cache TTL for completed historical periods.
- Safe period search through Fio's `periods` endpoint.
- Guarded access to Fio's `last` endpoint because it advances the bank-side
  download marker.
- Normalized transaction fields and bounded cursor pagination.

## Tool Surface

```text
fio_list_accounts
fio_test_connection
fio_find_transactions
fio_get_new_transactions
fio_get_metadata
```

See [TOOLS.md](TOOLS.md) for schemas and examples. See [DESIGN.md](DESIGN.md)
for rationale.

## Configuration

Single account:

```bash
FIO_API_TOKEN=replace-with-fio-api-token
FIO_ACCOUNT_ALIAS=main
FIO_ACCOUNT_LABEL="Main account"
```

Multiple accounts:

```bash
FIO_ACCOUNTS_JSON='[{"alias":"main","label":"Main account","token":"replace-with-token"}]'
```

Optional:

```bash
FIO_BASE_URL=https://fioapi.fio.cz/v1/rest/
FIO_TIMEOUT_SECONDS=30
FIO_RATE_LIMIT_SECONDS=31
FIO_CACHE_TTL_ACTIVE_SECONDS=600
FIO_CACHE_TTL_HISTORICAL_SECONDS=86400
FIO_CACHE_TTL_LAST_SECONDS=600
FIO_MAX_PERIOD_DAYS=31
```

Do not commit `.env` or real Fio API tokens.

## Running

```bash
mise run mcp
```

For local development:

```bash
mise run test
mise run lint
mise run format-check
mise run check
```

## Codex MCP Configuration

This repository includes a project-local Codex config at `.codex/config.toml`.
Codex sessions started in this repo will get MCP servers launched through
FastMCP reload:

```text
fio        -> mise run mcp-reload
simpleshop -> mise run simpleshop-mcp-reload
```

The Fio server reads this repo's local `.env` / `.env.e2e` settings through the
Python settings layer. The SimpleShop server runs from `../simpleshop-mcp`, so it
uses that package's lockfile and local `.env`. Real token files remain ignored by
git.

Live tests are opt-in:

```bash
FIO_E2E=1 mise run e2e
```

Live tests use safe `periods` calls only. `fio_get_new_transactions` is not
covered by default live tests because it advances Fio's last-download marker.
