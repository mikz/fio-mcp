# Tool Reference

Operational notes for maintainers running the fio-mcp server. All runtime
information that an LLM client needs to use the tools — parameter
descriptions, defaults, examples, detail-level visibility, message-pattern
catalog, error codes, side effects — is exported through the MCP JSON
Schema and the `fio_get_metadata` tool response. Call `fio_get_metadata`
once at session start to get the authoritative reference; this file is for
humans who maintain the server.

## Read-only contract

Bank-data tools are read-only. They never create, update, delete, send payments,
or accept credentials through MCP tool calls.

## Authentication and token storage

Configure accounts through environment or `.env`; no MCP tool accepts or stores
Fio credentials. Use `FIO_ACCOUNTS_JSON` for the full account registry.

Raw tokens are never returned by any tool or exposed in the JSON Schema.
External tools see safe `token_key` references only.

### Local token bootstrap

For local development only, set `FIO_TOKENS_JSON` to a JSON array of raw token
strings to seed the registry in memory at startup. Tokens are validated and
paired to accounts at boot, but never persisted.

```bash
export FIO_TOKENS_JSON='["raw-token-1","raw-token-2"]'
```

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `FIO_BASE_URL` | `https://fioapi.fio.cz/v1/rest/` | Override only for tests / sandboxes |
| `FIO_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `FIO_RATE_LIMIT_SECONDS` | `31` | Local per-token cooldown — keep ≥ 30 to match Fio's 30-second rule |
| `FIO_CACHE_TTL_ACTIVE_SECONDS` | `600` | Cache TTL for live data |
| `FIO_CACHE_TTL_HISTORICAL_SECONDS` | `86400` | Cache TTL for stable historical periods |
| `FIO_CACHE_TTL_LAST_SECONDS` | `600` | Cache TTL for `last`-endpoint snapshots |

## Cache architecture

- In-memory only; no on-disk cache.
- Cache keys include account, endpoint, date range, and detail-level
  raw/non-raw flag.
- Raw `periods` and `last` responses can satisfy later non-raw reads for
  the same account/date endpoint. Non-raw cache entries never satisfy raw
  reads.

## Side effects to remember

- `fio_get_new_transactions` advances Fio's bank-side last-download marker.
  Requires `confirm_advances_download_marker=true`. Marker is held by a
  dedicated account token; other reads load-balance across the remaining
  pool.
- No MCP tool mutates local credential storage.

## Troubleshooting

| Symptom | Diagnosis |
|---|---|
| `409 / rate_limited` | Same token used within the 30s window — wait `retry_after_seconds` or use cache |
| `invalid_token_or_url` | Token revoked or `FIO_BASE_URL` wrong |
| `cursor_expired` | Cursor snapshot evicted from memory; repeat the original query |
| `cursor_mismatch` | Filter set differs from the one that produced the cursor; restart pagination |
| `confirmation_required` | Called `fio_get_new_transactions` without `confirm_advances_download_marker=true` |
| `unknown_account` | Alias / bank_account not in the registry — call `fio_list_accounts` |
| `not_configured` | Configure `FIO_ACCOUNTS_JSON` or local `FIO_TOKENS_JSON` |
| `ambiguous_account` | More than one account configured; pass `account` explicitly |

The complete error-code catalog with remediation hints is in
`fio_get_metadata.error_codes`.
