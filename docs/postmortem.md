# Postmortem: risk scoring outage caused by a typo'd ConfigMap value

| | |
|---|---|
| Date | 2026-10-30 (planned incident exercise on the local kind cluster) |
| Duration | 2 min 45 s of no risk scoring (19:28:26 → 19:31:11 UTC) |
| Severity | Degraded: ingest, gateway and dashboard stayed up; no new transactions were scored or flagged |
| Data loss | None. Kafka retained the backlog and risk caught up after the fix |
| Raw evidence | [incident/incident-run.log](incident/incident-run.log), produced by [incident/run_incident.sh](incident/run_incident.sh) |

## Summary

Someone tuning the flag threshold edited the `risk` ConfigMap directly (`kubectl patch`) and typed
`5O` (letter O) instead of `50`. Nothing happened right away. The running pod had read its environment at
startup, so the bad value sat unnoticed. When the pod was next rescheduled (simulated as a node drain),
the replacement pod refused to start. Its config validation exited with code 78, and the pod went into
CrashLoopBackOff. The `risk` consumer group stopped consuming, so lag on `transactions.validated` grew
by about 10 msgs/s (the generator rate), and the dashboard's flagged feed went quiet.

## Timeline (UTC, from the log)

| Time | Event |
|---|---|
| 19:28:23 | Baseline: risk `1/1 Running`, consumer lag 0, 238 flagged transactions in Mongo |
| 19:28:24 | **Trigger.** ConfigMap edited: `RISK_FLAG_THRESHOLD: "50" → "5O"` |
| 19:28:25 | Still healthy: the running pod reads env only at start, so the change is latent |
| 19:28:26 | Pod rescheduled (`kubectl delete pod`, standing in for a node drain) |
| 19:28:41 | **Impact starts.** New pod `0/1 CrashLoopBackOff`, lag 163, flagged count frozen at 238 |
| 19:29:16 → 19:30:42 | Restarts 3 → 4 with growing backoff; lag 503 → 1,364 (linear, ~10 msgs/s); flagged count still 238 |
| 19:30:44 | **Diagnosis.** `kubectl get pods` shows CrashLoopBackOff, last exit code **78**; `kubectl logs --previous` shows the CRITICAL line below |
| 19:30:44 | **Fix.** ConfigMap restored to `"50"`, then `kubectl rollout restart deploy/risk` |
| 19:31:11 | **Recovered.** risk `1/1 Running`, lag **0**, flagged 238 → 265 as the backlog was scored |
| 19:31:11 → 19:32:36 | Steady state: lag 0, flagged count rising normally (265 → 277) |

## Detection

What would have caught it, in the order a responder sees the signals:

1. **Consumer lag** on group `risk` grew linearly from 0 to 1,364 in about two minutes
   (`kafka-consumer-groups.sh --describe --group risk`). This was the clearest signal.
2. **Pod state:** `CrashLoopBackOff` with restart count rising, and the Deployment eventually reports
   `ProgressDeadlineExceeded`.
3. **Business symptom:** the flagged count in Mongo flat-lined, and the live feed's WebSocket
   received no events.

There is no alerting stack in this project (no Prometheus or Alertmanager), so in this exercise a person
found it. That gap is follow-up item 3.

## Root cause and diagnosis

```json
{"level": "CRITICAL", "service": "risk", "logger": "risk",
 "msg": "invalid risk configuration, refusing to start",
 "errors": [{"field": "risk_flag_threshold",
             "msg": "Input should be a valid integer, unable to parse string as an integer"}]}
```

- **Immediate cause:** a non-numeric value in the `risk` ConfigMap.
- **Why it caused an outage:** the risk service validates its configuration at startup
  (`services/risk/src/risk/config.py`) and deliberately exits with code 78 (`EX_CONFIG`). It refuses to
  start, rather than scoring with a wrong threshold. That's the intended behaviour. Without it, a bad
  value could have flagged everything or nothing, and nobody would have noticed.
- **Why it wasn't caught earlier:**
  - The edit went around Helm (`kubectl patch`), so neither the chart's checksum annotation nor a rolling
    update tested the new config while the old pod was still serving.
  - ConfigMap changes don't restart pods, so the fault stayed latent until an unrelated reschedule.
  - The chart had no schema for config values.

**What went well:** the fail-fast design made diagnosis take seconds. One log line named the exact field.
No data was lost: Kafka kept the backlog, consumption resumed from the committed offset, and the
idempotent writes meant replaying was safe.

## Fix

Restore the value and restart:

```bash
kubectl patch cm risk -n ledgerlens --type merge -p '{"data":{"RISK_FLAG_THRESHOLD":"50"}}'
kubectl rollout restart deploy/risk -n ledgerlens
```

Lag was back to 0 within 30 s of the restart.

## Prevention

| # | Action | Status |
|---|---|---|
| 1 | `deploy/helm/risk/values.schema.json`: Helm rejects malformed thresholds at lint, template, install and upgrade time. With `--set risk.config.RISK_FLAG_THRESHOLD=5O`, `helm template` fails with `'5O' does not match pattern`. | **Done** (verified) |
| 2 | Change config only through Helm/Git, never `kubectl edit/patch`. The chart's `checksum/config` annotation then triggers a rolling update, and readiness keeps the old pod serving while a bad new pod crash-loops: a stalled rollout, not an outage. | Process; enforce with GitOps or RBAC in a real cluster |
| 3 | Alert on `risk` consumer lag > 500 for 2 min, and on `kube_deployment_status_condition{condition="Progressing",status="false"}` | Open: needs a metrics stack |
| 4 | Run `helm lint` (and with it the schema) in CI on every change to `deploy/` | Done: the CI `helm` job runs `scripts/helm_check.sh` |
| 5 | Runbook entry for "risk exits with code 78" | Done: [runbook.md](runbook.md) |

## Bonus finding from the same session (bring-up, not the exercise)

While bringing up the kind cluster, Couchbase was **OOMKilled within 2 s of starting**, even with a
2.5 GiB limit. The kernel's OOM log showed `beam.smp` (the Erlang VM) at 2.1 GB resident memory. Pods on the kind
node inherit containerd's open-files limit of **1,073,741,816**, while `docker run` gives 1,048,576, and the Erlang VM sizes its port table from that limit. The fix is
`ulimit -n 65536` before starting the server (`deploy/helm/infra/templates/couchbase.yaml`). The pod then
started at its original 1.5 GiB limit with 0 restarts. Raising the memory limit had only treated the symptom.
