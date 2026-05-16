# Tool Reference

All tools are read-only. They never create, update, delete, or send payments.

## Cache Modes

Every Fio-reading tool has explicit cache control:

```text
use      return valid cache, otherwise call Fio
refresh  bypass cache, call Fio, update cache
only     return cache only; never call Fio
```

`cache: "only"` returns `cache_miss` when no valid in-memory snapshot exists.

## `fio_list_accounts`

Lists configured aliases. Tokens are never returned.

```json
{
  "include_status": false
}
```

## `fio_test_connection`

Checks one account through a safe period read for today.

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
    "include_counterparty_details": true,
    "include_raw": false,
    "cache": "use"
  }
}
```

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
    "include_raw": false,
    "cache": "use"
  }
}
```

Without `confirm_advances_download_marker: true`, the tool returns
`confirmation_required`.

## `fio_get_metadata`

Returns column mappings, cache modes, error codes, rate-limit seconds, and the
configured maximum period size.

```json
{}
```
