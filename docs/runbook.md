# LedgerLens runbook

This runbook is for the local docker-compose stack. In Kubernetes the same checks apply: swap
`docker compose exec <svc>` for `kubectl exec deploy/<svc>`, and `docker compose logs <svc>`
for `kubectl logs deploy/<svc>`. All commands run from the repo root.

**Shell setup for Mongo commands.** Most commands below open a Mongo shell. Define this once:

```sh
mongo() { docker compose exec -T mongo sh -c 'mongosh --quiet -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin ledgerlens' ; }
# usage:  echo 'db.transactions.countDocuments({})' | mongo
```

**Kafka CLI.** It runs inside the broker container, using the internal listener `localhost:29092`:

```sh
kt() { docker compose exec -T kafka /opt/kafka/bin/"$@" --bootstrap-server localhost:29092 ; }
```

## Start and stop

| Goal | Command |
|---|---|
| First run | `cp .env.example .env`, then set `JWT_SECRET` (`openssl rand -hex 32`) and change the passwords |
| Core pipeline (Kafka, Mongo, Couchbase, generator, ingest, risk, gateway) | `make up` |
| Core + dashboard on http://localhost:4200 | `PROFILES="core ui" make up` |
| Core + query agent (needs the FAISS index, see below) | `make semantic-index && PROFILES="core ai" make up` |
| Run the nightly Spark job once | `make spark` (needs `core` running) |
| Everything | `make semantic-index && PROFILES="core ui ai" make up` |
| Status / logs | `make ps`, `make logs`, or `docker compose logs -f <service>` |
| Stop (keeps data volumes) | `PROFILES="core ui ai" make down` |
| Wipe all data | `docker compose --profile core --profile ui --profile ai down -v` |

`ui`, `ai` and `spark` all depend on services in `core`, so always include `core`.

**RAM.** These limits are configured, not measured:

- Kafka heap: 512 MB (`KAFKA_HEAP_OPTS`).
- Couchbase: 512 MB data quota plus 256 MB index quota (`couchbase-init.sh`), on top of its own process overhead.
- Spark job: capped at 2 GB (`mem_limit`).
- Mongo: WiredTiger takes about half of the RAM Docker gives it unless capped.
- Agent: loads torch and the embedding model.

Give Docker at least 8 GB for `core` plus one extra profile. On a smaller machine, run `core` and add the other profiles one at a time.

## Health endpoints

| Service | Live | Ready checks | Port |
|---|---|---|---|
| ingest | `GET /health/live` | Mongo ping, Kafka metadata, consumer task alive (+ `stats`) | 8000 |
| risk | `GET /health/live` | Couchbase KV ping, Mongo ping, consumer thread alive (+ `stats`) | 8001 |
| gateway | `GET /health/live` | Mongo ping, Kafka metadata. Risk and agent are **not** checked, so they can't take the gateway down. | 8002 |
| agent | `GET /health/live` | Mongo ping | 8003 |
| frontend | `GET /` | none | 4200 (host) |

`curl -s localhost:8000/health/ready | jq` shows the failing check by name: `"mongo": "error: ServerSelectionTimeoutError"`.

## Common failures

### Consumer lag: transactions stop appearing, or appear late

- **Symptoms.** The dashboard feed goes quiet, and the `/v2/transactions` newest timestamp falls behind the wall clock.
- **Diagnose.**

  ```sh
  kt kafka-consumer-groups.sh --describe --group ingest   # LAG per partition
  kt kafka-consumer-groups.sh --describe --group risk
  curl -s localhost:8000/health/ready | jq .stats         # inserted / dlq / duplicates
  docker compose logs --since 5m ingest | grep -i "batch failed"
  ```

- **Fix.** A growing lag with `batch failed, will retry` in the logs means a downstream write is failing. The consumer rewinds and retries with backoff (1s doubling to 30s) and never skips. Fix the dependency (usually Mongo or Couchbase) and the lag drains on its own. A lag with no errors means the consumer is just slower than the producer: lower `GEN_RATE_PER_SEC`, or scale consumers (up to the 6 partitions).

### Bad messages: DLQ growing

```sh
kt kafka-get-offsets.sh --topic transactions.dlq
docker compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server localhost:29092 \
  --topic transactions.dlq --from-beginning --max-messages 5 --property print.headers=true
```

Each message carries `reason` (`invalid_json` or `schema_validation_failed`), `detail` (which field and why), the `raw` payload and the `source` partition and offset. A small steady trickle is expected: the generator deliberately injects malformed messages (`GEN_MALFORMED_RATE`, default 0.002). A sudden jump means a producer started sending a different schema; the `detail` field names which field.

#### Replaying the DLQ

There is no replay tool. The manual procedure is:

1. Export the dead letters:
   `kafka-console-consumer.sh … --topic transactions.dlq --from-beginning --timeout-ms 10000 > dlq.jsonl`
2. Extract and fix the payloads (`jq -r .raw dlq.jsonl`). Only replay messages whose cause is fixed; a message that is still invalid just lands in the DLQ again.
3. Produce the corrected payloads back to `transactions.raw` with `kafka-console-producer.sh --topic transactions.raw`. To keep per-customer ordering, set the key to the customer_id with `--property parse.key=true --property key.separator=|` and lines of the form `C000123|{...}`.

Re-delivering a txn_id that is already stored is harmless, because ingest dedupes on `_id`.

### Slow Mongo queries / API latency

```sh
echo 'db.currentOp({active: true, secs_running: {$gte: 1}}).inprog.map(o => [o.ns, o.secs_running, o.planSummary])' | mongo
echo 'db.transactions.getIndexes().map(i => i.name)' | mongo   # expect _id_, customer_ts, merchant_ts, flagged_ts, ts_id
echo 'db.transactions.find({customer_id: "C000001"}).sort({timestamp: -1}).limit(50).explain("executionStats").queryPlanner.winningPlan' | mongo
```

`COLLSCAN` in `planSummary` or `winningPlan` means an index is missing. Each index is created at startup by the service that owns it:

| Index | Owner |
|---|---|
| `customer_ts`, `merchant_ts` | ingest |
| `flagged_ts` | risk |
| `ts_id` | gateway |

Restarting the owner recreates its index, or you can recreate it by hand, e.g.
`db.transactions.createIndex({customer_id: 1, timestamp: -1}, {name: "customer_ts"})`.
To catch slow queries going forward: `db.setProfilingLevel(1, {slowms: 100})`, then read `db.system.profile`. For a with/without-index comparison, run `make bench` (results in `benchmarks/results/`).

### Risk service exits immediately (exit code 78)

- **Symptoms.** `docker compose ps risk` shows `Exited (78)`, or the pod is in `CrashLoopBackOff`. Transactions stop gaining a `risk` field.
- **Diagnose.** `docker compose logs risk | grep CRITICAL` shows
  `"msg": "invalid risk configuration, refusing to start", "errors": [{"field": ..., "msg": ...}]`.
  Exit 78 is `EX_CONFIG`: config is validated before gunicorn starts (`services/risk/src/risk/app.py`), so a bad value can't turn into a silently mis-scoring service.
- **Fix.** Correct the named `RISK_*` value (in Kubernetes, the ConfigMap; then roll the pods). The validated fields are in `services/risk/src/risk/config.py`. One cross-field check fails if `RISK_FLAG_THRESHOLD` is higher than the sum of the rule weights, because then nothing could ever be flagged.
- **Compose caveat.** The `risk` service only receives the variables listed under its `environment:` key. Putting `RISK_*` in `.env` alone has no effect; add them to that list.

### Couchbase not ready

- **Symptoms.** `risk` doesn't start, or `/health/ready` on 8001 shows `"couchbase": "error: ..."`.
- **Diagnose.**

  ```sh
  docker compose logs couchbase-init      # expect "cluster already initialised" / "bucket already exists" on re-runs
  set -a; . ./.env; set +a
  curl -s -u "$COUCHBASE_USER:$COUCHBASE_PASSWORD" localhost:8091/pools/default | jq .nodes[0].status   # "healthy"
  ```

- **Fix.** Couchbase takes a while on a cold start. `risk` waits for `couchbase-init` to complete, then gives the SDK 30s (`wait_until_ready`). If init failed, rerun `docker compose up couchbase-init`; it is idempotent. If the console shows the node out of memory, Docker needs more RAM. A missing `idx_profile_risk` index only breaks `/reports/top-risky`, and restarting `risk` recreates it.

### Gateway returns 502 / 503 / 504

The gateway's error body names the failing upstream: `{"code": "upstream_unavailable", "message": "risk service is unavailable", "request_id": ...}`.

| Code | Meaning | Check |
|---|---|---|
| 503 `upstream_unavailable` | connection refused, or connect timeout (1s) | `curl localhost:8001/health/ready` / `:8003`; is the `ai` profile running? |
| 504 `upstream_timeout` | upstream accepted but didn't answer in time (risk 3s, agent 30s) | upstream logs for that `request_id` |
| 502 `upstream_error` | upstream returned 5xx, or the connection broke | upstream logs; an unhandled exception there logs a traceback |

Follow one failing request across services with `docker compose logs gateway risk agent | grep <request_id>`.

Two related behaviours:

- **Retries.** GETs are retried up to 3 times with jittered backoff. A POST to the agent is retried only on connect failures, never after a timeout, so a slow LLM call can't produce duplicate audit entries.
- **Customer page degrades.** `/v2/customers/{id}` still returns 200 when risk is down, with `risk_profile_error` set.

### WebSocket live feed disconnects

- **Close code 1008 right after connecting.** The `?token=` JWT is missing, invalid or expired. Log in again (dev tokens last 8h).
- **Reconnect loop.** The frontend retries with backoff of 1s, 2s, 4s… capped at 30s, and resets after a message arrives. Check `curl localhost:8002/health/ready`, and check `docker compose logs frontend` for nginx upstream errors. nginx keeps idle sockets open for 1h (`proxy_read_timeout`).
- **Missing events.** Each client has a bounded queue (`WS_QUEUE_SIZE`, default 100), and a slow client loses its oldest messages rather than stalling everyone. The gateway reads `transactions.flagged` from `latest` with no consumer group, so events published while a gateway was restarting are not replayed to the feed. They are still in Mongo, under `/v2/transactions?flagged=true`.

### Agent queries rejected or failing

```sh
echo 'db.audit_log.find({allowed: false}, {_id: 0, user: 1, role: 1, question: 1, reason: 1, timestamp: 1}).sort({timestamp: -1}).limit(10)' | mongo
```

The `reason` field says why. Common reasons:

- `collection 'audit_log' is not readable by role 'analyst'`: RBAC working as intended.
- `pipeline stage 1: $out is not an allowed read-only stage` or `forbidden operator(s): $where`: the planner produced a non read-only pipeline. That's expected from injection attempts. If it shows up for normal questions with the LLM planner, look at the generated `pipeline` in the same entry.
- `execution: ...`: Mongo rejected the pipeline or hit `AGENT_TIMEOUT_MS` (default 5000).

Wrong answers to allowed queries usually mean retrieval picked the wrong term; compare `terms_used` with the question. `make eval` measures this.

**Which planner is running.** The agent uses the ADK LLM planner only when a key is set: `GOOGLE_API_KEY` for Gemini, or `ANTHROPIC_API_KEY` when `AGENT_MODEL=claude-*`. Otherwise it falls back to the deterministic catalog planner. The startup log line `agent started` shows which one.

### Editing the business catalog (FAISS index stale)

- **Symptoms.** After `semantic/catalog.yaml` changes, the agent fails at startup with `semantic index is stale: catalog.yaml changed, run ledgerlens-build-index`. The index stores the catalog's SHA-256, so an index never silently serves old definitions.
- **Fix.**

  ```sh
  make semantic-index                 # validates the catalog (Pydantic) and rebuilds semantic/index/
  docker compose restart agent        # compose mounts ./semantic read-only into the container
  ```

The first build downloads the embedding model from Hugging Face; offline machines need the model cached (`HF_HOME`).

### Re-running the Spark aggregates

`make spark` runs the job once against the compose Mongo. Each run fully recomputes and overwrites `agg_daily_category_spend`, `agg_merchant_flagged_rate` and `agg_top_customers`, so re-running after a failure is safe. Check the result with `echo 'db.agg_merchant_flagged_rate.countDocuments({})' | mongo` and look at `computed_at`. If `flagged_txns` is 0 everywhere, the risk service hadn't scored anything when the job ran.

## Rotating the JWT secret

There is one HS256 secret and no key id, so rotating it invalidates every issued token.

1. Generate a new secret: `openssl rand -hex 32`.
2. Install it:
   - **Compose:** set `JWT_SECRET` in `.env`, then `docker compose up -d gateway`.
   - **Kubernetes:** update the `JWT_SECRET` key in the `ledgerlens-secrets` Secret, then `kubectl rollout restart deploy/gateway`.
3. Users log in again. During a rolling restart, replicas briefly hold different secrets, so some requests get 401 `unauthorized` until the rollout finishes.

Zero-downtime rotation would need the gateway to accept a list of verification keys selected by a `kid` header. That isn't implemented.

## Where things are

| What | Where |
|---|---|
| Measured numbers (index explain, risk precision/recall, agent eval) | `benchmarks/results/` |
| API contract, v1 vs v2 | `docs/api-versioning.md` |
| Architecture and data model | `docs/architecture.md` |
| Kubernetes / OpenShift / Harness | `deploy/README.md` |
