# Fio MCP

Read-only [FastMCP](https://gofastmcp.com/) server for Fio Bank transaction data.

The server is designed for accounting and operations agents that need to inspect
bank movements without mutating bank state. It uses Fio's read APIs only.

## Features

- Runtime token setup with the form-backed `fio_add_token`; accounts can hold
  multiple tokens.
- No raw token reader parameters, responses, cache keys, or intentional logs.
- Per-token pacing with a 31 second local lease for Fio's 30 second guidance.
- Safe `periods` reads load-balance across an account's token pool.
- Explicit cache control: `use`, `refresh`, or `only`.
- Longer in-memory cache TTL for completed historical periods.
- Safe period search through Fio's `periods` endpoint.
- Guarded access to Fio's `last` endpoint because it advances the bank-side
  download marker.
- Normalized transaction fields and bounded cursor pagination.

## Tool Surface

```text
fio_list_accounts
fio_add_token
fio_alias_account
fio_remove_token
fio_test_connection
fio_find_transactions
fio_get_new_transactions
fio_get_metadata
```

See [TOOLS.md](TOOLS.md) for schemas and examples. See [DESIGN.md](DESIGN.md)
for rationale.

## Runtime Setup

The server starts without Fio credentials. Add tokens through the setup tools:

```text
fio_add_token(prompt)
fio_alias_account(account, alias)
fio_remove_token(account, token_key)
```

`fio_add_token` is a FastMCP Apps form, matching the SimpleShop login flow.
Clients that support Apps render an inline form for the Fio API token and an
optional account alias. The submit callback validates the token with a safe
`periods` read for today, derives the real Fio account identity from the
response, and adds the token to that account's token pool. If only one account
is configured, transaction tools can omit `account`; otherwise pass either the
account alias or canonical `account_key` from `fio_list_accounts`.

Successful setup is stored in a credential store scoped to the server process
`cwd`. The OS keyring service is `fio-mcp:<scope-id>`, account `accounts`, where
`scope-id` is derived from the canonical cwd. The private fallback file is:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/fio-mcp/scopes/<scope-id>/accounts.json
```

The fallback file is written with mode `0600`. Legacy unscoped stores such as
`${XDG_CONFIG_HOME:-$HOME/.config}/fio-mcp/accounts.json` are not read.

For headless pre-seeding, provide only a JSON array of raw tokens. The server
validates them on startup through the same path as `fio_add_token` and then
stores the derived account registry locally.

```bash
FIO_TOKENS_JSON='["replace-with-fio-token","replace-with-another-token"]'
```

Do not put the full internal account registry in environment variables.

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
