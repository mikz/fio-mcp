# Tool Reference

Bank-data tools are read-only. They never create, update, delete, or send
payments. Setup tools only mutate local credential configuration.

## Cache Modes

Every Fio-reading tool has explicit cache control:

```text
use      return valid cache, otherwise call Fio
refresh  bypass cache, call Fio, update cache
only     return cache only; never call Fio
```

`cache: "only"` returns `cache_miss` when no valid in-memory snapshot exists.

## Detail Levels

Transaction tools support explicit response shaping through `detail_level`:

```text
summary       payment summary fields only; hides names, accounts, free text, and raw payloads
counterparty  summary fields plus structured counterparty account/name/bank fields
full          all normalized non-raw fields, including bank free text
raw           full plus the raw Fio transaction payload
```

For pairing with SimpleShop or Darujme, prefer `detail_level: "summary"`.
It returns `transaction_id`, dates, amount, currency, direction, payment symbols,
transaction type, and bank order ID. It intentionally omits `message`,
`comment`, `user_identification`, and payer reference because banks can place
customer names in those fields.

`detail_level` is the only response-shaping input. Use `detail_level: "raw"` to
include raw Fio payloads; the old compatibility flags are not accepted.

## `fio_login`

Collects a Fio API token through one setup tool. `mode` accepts `auto`,
`direct`, `prefab`, or `web`. `auto` uses Prefab when the MCP client advertises
Apps UI support, otherwise it returns a localhost web-login URL. `direct` accepts
the token and optional alias in the `credentials` object.

```json
{
  "mode": "direct",
  "credentials": {
    "token": "fio-token",
    "alias": "main"
  }
}
```

The form asks for:

- `token`: Fio API token from Fio internet banking
- `alias`: optional friendly account alias, for example `main`

The token is checked with a safe `periods` read for today. The response account
metadata is used to pair the token to an account token pool. The raw token is
stored in keyring plus a private fallback file and is never returned. The
exposed MCP tool schema does not contain a raw `token` argument.

Response:

```json
{
  "ok": true,
  "account": "2603445200-2010-CZK",
  "account_key": "2603445200-2010-CZK",
  "alias": null,
  "token_key": "a1b2c3d4e5f6a7b8",
  "token_count": 1,
  "added": true
}
```

Adding the same token again is idempotent and reports that it is already
configured.

For headless startup, `FIO_TOKENS_JSON` may contain a JSON array of raw token
strings. Full account-registry JSON is intentionally not accepted through env;
startup tokens are validated and paired to accounts at runtime.

## `fio_alias_account`

Assigns a friendly alias to an account. `account` can be either the current
alias or the canonical `account_key` from `fio_list_accounts`.

```json
{
  "account": "2603445200-2010-CZK",
  "alias": "main"
}
```

After aliasing, transaction tools can use `"account": "main"`.

## `fio_remove_token`

Removes a token by safe `token_key`; raw tokens are not needed for removal.

```json
{
  "account": "main",
  "token_key": "a1b2c3d4e5f6a7b8"
}
```

The last token on an account cannot be removed. If the removed token was the
marker token, the oldest remaining token becomes the marker token.

## `fio_list_accounts`

Lists configured accounts, optional token keys, and optional pool status. Raw
tokens are never returned.

```json
{
  "include_tokens": true,
  "include_status": true
}
```

Example account entry:

```json
{
  "account": "main",
  "account_key": "2603445200-2010-CZK",
  "alias": "main",
  "account_id": "2603445200",
  "bank_id": "2010",
  "currency": "CZK",
  "token_count": 2,
  "marker_token_key": "a1b2c3d4e5f6a7b8",
  "configured": true,
  "tokens": [
    {
      "token_key": "a1b2c3d4e5f6a7b8",
      "role": "marker",
      "available": true,
      "next_available_at": null
    }
  ]
}
```

## `fio_test_connection`

Checks one account through a safe period read for today. If exactly one account
is configured, `account` can be omitted.

```json
{
  "account": "main",
  "cache": "use"
}
```

## `fio_find_transactions`

Main transaction search tool. Uses the Fio `periods` endpoint and does not
advance the bank-side last-download marker.

```json
{
  "query": {
    "account": "main",
    "date_from": "2026-05-01",
    "date_to": "2026-05-16",
    "direction": "incoming",
    "currency": "CZK",
    "variable_symbol": "2026000001",
    "counterparty_search": "Novak",
    "min_amount": "100.00",
    "max_amount": "1000.00",
    "limit": 100,
    "cursor": null,
    "detail_level": "summary",
    "cache": "use"
  }
}
```

If exactly one account is configured, `account` can be omitted. If more than one
account is configured, omitted account selection returns `ambiguous_account`.
Safe period reads load-balance across the account's token pool.

Date ranges are capped by `FIO_MAX_PERIOD_DAYS` to avoid expensive failed calls
under Fio's rate limit. Raise that setting when a wider period is intentional.

## `fio_get_new_transactions`

Fetches from Fio's `last` endpoint. This endpoint advances Fio's bank-side
download marker, so confirmation is required.

```json
{
  "request": {
    "account": "main",
    "confirm_advances_download_marker": true,
    "limit": 100,
    "cursor": null,
    "detail_level": "summary",
    "cache": "use"
  }
}
```

Without `confirm_advances_download_marker: true`, the tool returns
`confirmation_required`. This tool uses only the account's `marker_token_key`;
it does not load-balance across read tokens.

## `fio_get_metadata`

Returns the machine-readable tool contract: column mappings, cache modes, detail
levels, direction values, error codes, operational limits, side effects,
rate-limit seconds, and the configured maximum period size.

```json
{}
```

The `limits` object includes:

```text
max_page_limit      maximum accepted limit for paged transaction responses
max_period_days     maximum accepted Fio period range
rate_limit_seconds  local per-token cooldown
```

The `side_effects` list identifies guarded operations. Currently only
`fio_get_new_transactions` has a bank-side side effect: it advances Fio's
last-download marker and requires `confirm_advances_download_marker=true`.

Known error codes:

```text
invalid_token_or_url   check the configured token and base URL
rate_limited           Fio returned 409; retry after cooldown or use cache
too_many_transactions  reduce date range or add narrower filters
invalid_request        check dates, account config, and request parameters
cache_miss             cache=only has no valid in-memory snapshot
cursor_expired         repeat the original query to create a fresh cursor
confirmation_required  last endpoint requires explicit marker confirmation
period_too_large       reduce range or raise FIO_MAX_PERIOD_DAYS intentionally
invalid_cursor         discard cursor and repeat the original query
cursor_mismatch        repeat the exact filters used to create the cursor
unknown_account        call fio_list_accounts and retry with alias or account_key
not_configured         call fio_login first
ambiguous_account      pass account because multiple accounts are configured
unknown_token          call fio_list_accounts with include_tokens=true
last_token             add another token before removing this one
network_error          Fio API network request failed
error                  unexpected local fallback
```
