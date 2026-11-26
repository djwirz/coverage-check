# The mock payer

A payer of our own that misbehaves on demand (ADR 0006). The protocol and what it records are in [ADR 0009](adr/0009-payer-protocol-and-evidence.md). Everything about it is synthetic: the members are [synthetic-cohort](https://github.com/djwirz/synthetic-cohort)'s, and the plans and benefits are made up.

## Protocol

`POST /eligibility`, with an `Idempotency-Key` header (`<check id>:<cycle>`, [ADR 0010](adr/0010-one-payer-key-per-check.md)) and this body:

```json
{
  "member_id": "...",
  "given": ["..."],
  "family": "...",
  "birth_date": "1992-06-16",
  "payer": "...",
  "service_date": "2026-11-01"
}
```

A 200 is the payer's answer:

```json
{
  "result": "active",
  "plan": {"name": "... synthetic plan"},
  "coverage_period": {"start": "2026-06-16", "end": "2027-06-16"},
  "benefits": [{"service": "primary_care_visit", "copay_usd": 20}]
}
```

| `result` | When |
|---|---|
| `active` | the service date is in the member's coverage period (start included, end not) |
| `inactive` | the member is known and the date is outside the period |
| `member_not_found` | no member with this id, birth date and payer, or the cohort labels them `missing` |
| `multiple_matches` | the cohort labels them `ambiguous` |

Names are carried but not matched. Anything but an answer has the body `{"error": "..."}`:

| Status | `error` | Meaning |
|---|---|---|
| 400 | `invalid_request` | a missing or unknown field, or no usable key |
| 409 | `in_progress` | this key is still being handled |
| 422 | `key_reused` | this key came before with another body |
| 429 | `rate_limited` | over a limit, or throttled; `Retry-After` is in seconds |
| 500, 502, 503 | `server_error` | the payer failed |
| 504 | `timed_out` | the payer gave up after a long wait |

A key still being handled gets 409, even if its answer is already decided. After that, a key that has an answer gets the same answer again, and is not billed again. Errors store nothing, so the same key can be tried again.

## Limits

- At most `rate_limit_per_second` requests in any one-second window. Every request received counts, refused ones too.
- At most `max_in_flight` being handled at once. A request stays in flight until the payer has finished with it, even if the caller has hung up.

## Failure profile

A TOML file, named by `MOCK_PAYER_PROFILE`: [`default.toml`](../config/mock-payer/default.toml) is a moderately bad day, [`calm.toml`](../config/mock-payer/calm.toml) has no faults. Each `*_rate` is the share of accepted new requests that get that fault.

| Setting | What the payer does | Billed |
|---|---|---|
| `throttle_rate` | 429 with `Retry-After`, though under the limit | no |
| `server_error_rate` | 500, 502 or 503 | no |
| `malformed_rate` | 200 with a body cut off mid-JSON | no |
| `slow_rate` | answers after an extra `slow_seconds` | yes |
| `timeout_rate` | holds for `timeout_seconds`, then 504 | no |
| `late_rate` | decides and bills at once, replies after `late_seconds` | yes |

Faults are repeatable: each is drawn from a hash of the seed, the request body and how many times that body has been seen. The same run gets the same faults in any order and with any attempt ids. Which requests hit the rate or in-flight limit does depend on timing.

## Request log

`GET /requests?after=<id>&limit=<n>` returns every eligibility request received: key, body hash, `received_at` and `replied_at` (seconds since 1970), status, behaviour, whether it was a replay, whether it was billable. No member details. `behaviour` is `ok` or the fault's name (`throttle`, `server_error`, `malformed`, `slow`, `timeout`, `late`), or `replay`, `rate_limit`, `in_flight_limit`, `invalid_request`, `in_progress`, `key_reused`, or `aborted` if the payer stopped while handling it. A row with no `replied_at` was never answered. It is kept in SQLite (`MOCK_PAYER_DB`) and survives a restart; deleting that file starts a new run. `GET /healthz` is not limited or logged.

## Where the failure modes come from

The shapes follow public descriptions. **The rates, delays and limits are our own choices**, picked to make a run eventful, not measured from any payer.

| Source (read 2026-11-26) | What we took from it |
|---|---|
| [RFC 6585 §4](https://www.rfc-editor.org/rfc/rfc6585#section-4) | 429 Too Many Requests, which may carry `Retry-After` |
| [RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110#field.retry-after) | `Retry-After` in seconds (it may also be a date; the mock sends seconds) |
| [IETF Idempotency-Key draft](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/) (not yet an RFC) | 409 for a key still being processed, 422 for a key reused with another payload |
| [Stedi API reference](https://www.stedi.com/docs/healthcare/api-reference), a public clearinghouse | limits per account on both requests per second and requests in flight, refused with 429 |
| [Stedi: send eligibility checks](https://www.stedi.com/docs/healthcare/send-eligibility-checks) | payers can take up to about a minute, so slow replies and timeouts are normal |
| [Stedi: eligibility troubleshooting](https://www.stedi.com/docs/healthcare/eligibility-troubleshooting) | payer connectivity failures are worth retrying; some errors are not |

5xx errors, malformed bodies and replies after the caller gave up are ordinary HTTP failures we chose to include; none of these sources is the reason for their form.
