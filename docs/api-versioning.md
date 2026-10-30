# API versioning: /v1 vs /v2

The gateway serves both versions side by side from the same MongoDB data. `/v1` is frozen
for existing clients; `/v2` introduces deliberate breaking changes. Both are pinned by
contract tests (`services/gateway/tests/test_contract.py`) that assert exact field sets.

## Breaking changes in v2

| Aspect | v1 | v2 | Why |
|---|---|---|---|
| Pagination | `?page=&page_size=` (offset) | `?cursor=&limit=` (keyset) | Offset pagination costs O(offset) (`skip` walks every earlier row) and drifts when new rows arrive mid-scroll. A cursor is a seek on the `(timestamp, _id)` index. |
| Envelope | `{items, page, page_size, total}` | `{data, next_cursor}` | `total` needs a `count_documents` over the whole filter on every request; v2 drops it. `next_cursor` is `null` on the last page. |
| Risk | flat `risk_score`, `flagged`, `reason_codes: [str]` | nested `risk: {score, flagged, reasons: [{code, detail}], scored_at}` or `null` | v1 can't tell "not scored yet" from "scored, not flagged" (both `flagged: false`); v2 uses `risk: null`. v2 also carries reason details and the scoring time. |
| Money | `amount: 12.3` (JSON number, i.e. float) + `currency` | `money: {amount: "12.30", currency}` | Floats can't represent most decimal amounts exactly. v2 sends an exact decimal string. |

Unchanged in both versions: auth (Bearer JWT), filters (`customer_id`, `merchant`, `flagged`,
`from`, `to`), sort order (newest first, `txn_id` as tie-break) and the error schema.

## v2-only endpoints

- `GET /v2/customers/{customer_id}`: recent transactions plus the risk profile from the risk
  service. If the risk service is down, it still returns 200 with `risk_profile: null` and
  `risk_profile_error: {code, message}`.
- `GET /v2/reports/top-risky?limit=`: proxied to the risk service (SQL++ report).
- `POST /v2/agent/query {question}`: proxied to the agent service. `user` and `role` come from
  the JWT, never from the request body.
- `GET /v2/audit` (admin only): the agent's query audit log.
- `WS /v2/ws/flagged?token=<jwt>`: live stream of flagged `RiskAssessment` events.

## The cursor

`next_cursor` is base64url of `["<timestamp ISO-8601>", "<txn_id>"]`. Clients must treat it as
opaque. The next page is "strictly after" that position:
`timestamp < ts OR (timestamp == ts AND _id < txn_id)`. The `_id` tie-break matters because
many transactions share a millisecond timestamp. Without it, rows at a page boundary would be
skipped or repeated. A malformed cursor returns `400 invalid_cursor`.

## Errors (both versions)

Every error, whether validation, auth, not-found, upstream or internal, has the same shape:

```json
{"code": "upstream_unavailable", "message": "risk service is unavailable", "request_id": "3f2c..."}
```

`request_id` matches the `X-Request-ID` response header. It is forwarded to the risk and agent
services, so a single id can be traced through every service's JSON logs.

| status | code | when |
|---|---|---|
| 400 | `invalid_cursor` | malformed v2 cursor |
| 401 | `unauthorized` / `token_expired` | missing, invalid or expired JWT |
| 403 | `forbidden` | role not allowed (e.g. analyst calling `/v2/audit`) |
| 404 | `not_found` | unknown transaction or route |
| 422 | `validation_error` | bad query or body |
| 502 | `upstream_error` | upstream returned 5xx or the connection broke |
| 503 | `upstream_unavailable` | upstream refused the connection |
| 504 | `upstream_timeout` | upstream timed out |

## Upstream retry policy

Calls to risk and agent use `httpx` timeouts (connect 1s; 3s for risk, 30s for agent) and up to
3 retries with exponential backoff and jitter (`0.2s · 2^attempt · U(0.5, 1.5)`).

- **GET** (idempotent): retried on connect errors, timeouts and 5xx.
- **POST** (`/agent/query`): retried **only** on connect failures, where the request provably
  never reached the server. A POST that timed out or returned 5xx may already have run (and
  written an audit-log entry), so it is not retried.
- 4xx responses are never retried; the upstream's `code` and `message` are passed through.

## Deprecation plan (when v1 is retired)

1. Add `Deprecation` and `Sunset` response headers to `/v1`.
2. Watch `/v1` traffic in the access logs until it reaches zero.
3. Remove the router.
