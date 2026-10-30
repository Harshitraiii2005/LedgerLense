# Deploying LedgerLens

| Target | How | Status |
|---|---|---|
| Local Kubernetes (kind) | `make deploy-local` | automated (build → load → Secret → Helm → smoke test) |
| OpenShift | [openshift/README.md](openshift/README.md) | **HUMAN ACTION**: needs a sandbox login |
| Harness CD | [harness/README.md](harness/README.md) | **HUMAN ACTION**: needs a Harness account + connectors |

## Layout

```
deploy/
  helm/
    ledgerlens-lib/     library chart: the one Deployment/Service/ConfigMap/HPA/CronJob template set
    ingest/ generator/ risk/ gateway/ agent/ frontend/ spark-aggregates/
                        one chart per service = values.yaml + `include "ledgerlens.app"` (or .cronjob)
    infra/              single-node Kafka (KRaft), MongoDB, Couchbase CE for kind (toggle: infra.enabled)
    ledgerlens/         umbrella chart (file:// deps) + values-kind.yaml + OpenShift Routes
  kind/cluster.yaml     1-node cluster, NodePorts 30802 -> localhost:8002 (gateway), 30880 -> :8080 (UI)
  openshift/            values-openshift.yaml + README (SCC / arbitrary UID notes)
  harness/              pipeline.yaml, service.yaml, values-harness.yaml + README
scripts/
  deploy_local.sh       what `make deploy-local` runs
  smoke_test.sh         waits for Ready, port-forwards, checks every /health/ready, calls /v1 + /v2, asserts data flows
  helm_deps.sh          vendors chart dependencies (lib -> service charts -> umbrella)
  helm_check.sh         helm lint --strict + kubeconform on the rendered umbrella (kind + OpenShift overlays)
```

## Kubernetes objects per service

| Service | Kind | Port | Probes | HPA | Secret keys |
|---|---|---|---|---|---|
| ingest | Deployment + Service + ConfigMap | 8000 | /health/live, /health/ready | CPU 70%, 1–3 | MONGO_URI |
| generator | Deployment + ConfigMap | – (worker) | – | – | – |
| risk | Deployment + Service + ConfigMap | 8001 | /health/live, /health/ready | – | MONGO_URI, COUCHBASE_USER, COUCHBASE_PASSWORD |
| gateway | Deployment + Service + ConfigMap | 8002 | /health/live, /health/ready | CPU 70%, 1–4 | MONGO_URI, JWT_SECRET |
| agent | Deployment + Service + ConfigMap | 8003 | /health/live, /health/ready | – | MONGO_URI, GOOGLE_API_KEY?, ANTHROPIC_API_KEY? |
| frontend | Deployment + Service + ConfigMap | 8080 | / | – | – |
| spark-aggregates | CronJob (02:00 nightly) + ConfigMap | – | – | – | MONGO_URI |

Every pod: `runAsNonRoot`, seccomp `RuntimeDefault`, all capabilities dropped, no privilege escalation,
read-only root filesystem with an `emptyDir` per `writablePaths` entry (default `/tmp`), requests + limits,
and a `checksum/config` annotation so a ConfigMap change rolls the pods. No chart sets `runAsUser`
for our own images, which is what lets the same charts run under OpenShift's arbitrary UIDs.

Consumers get a `wait-for-topics` initContainer (Kafka CLI) so they never start before their topics
exist: with auto-create off, an aiokafka consumer that subscribes first sees no partitions until its
next metadata refresh (~5 minutes).

Secrets: one Secret (`ledgerlens-secrets`) referenced by key, created out-of-band. `make deploy-local`
builds it from `.env` with `kubectl create secret --dry-run=client -o yaml | kubectl apply -f -`.
Nothing secret is in git or in any values file.

## Infra choice: in-repo StatefulSets, not community charts

The spec allows well-known community charts for Kafka/Mongo/Couchbase. I checked the obvious candidates
(2026-10-30) and chose small in-repo StatefulSets using the **same pinned official images as docker-compose**:

- **Bitnami** (`bitnami/kafka`, `bitnami/mongodb`): since Bitnami's 2025 catalog change, versioned tags
  are gone from the free `bitnami/` namespace (`docker manifest inspect bitnami/kafka:3.9.0` → not found;
  the frozen copy is `bitnamilegacy/kafka:3.9.0`, which receives no updates). Pinning a demo to an
  unmaintained mirror is worse than owning ~150 lines of YAML.
- **Couchbase Autonomous Operator**: targets Couchbase Server Enterprise Edition; this project uses CE.
- **Strimzi / MongoDB operator**: the right answer for a real cluster (and what the OpenShift overlay
  assumes), but each adds an operator pod, CRDs and webhooks — too much RAM for a laptop kind cluster.

Trade-off accepted: single replica, no TLS, no backups — `infra` is a dev convenience, switched off with
`infra.enabled=false` (OpenShift/Harness overlays do this) and services pointed at managed endpoints.

## Versions

| Component | Version | Notes |
|---|---|---|
| Helm | 4.3.1 | charts are `apiVersion: v2`, Helm 3 compatible |
| kind | 0.33.0 | |
| kubeconform | 0.8.0 | validated against Kubernetes schema 1.33.0 |
| Kafka image | apache/kafka:3.9.1 | KRaft, also used for topic Job + wait-for-topics |
| MongoDB image | mongo:8.0.34 | WiredTiger cache capped at 0.25 GB |
| Couchbase image | couchbase/server:community-8.0.0 | data + index + query |
| Service images | ledgerlens/&lt;svc&gt;:dev | built from the repo `Dockerfile` (SERVICE=…) |
| Spark image | ledgerlens/spark-aggregates:dev | `jobs/spark_aggregates/Dockerfile` (apache/spark 3.5.7) |
| Frontend image | ledgerlens/frontend:dev | `frontend/Dockerfile` (expected: nginx-unprivileged on 8080) |

## Resource budget (kind)

Sum of the chart **requests / limits** with every component enabled — an estimate from the YAML,
not a measurement:

| | CPU request | Memory request | Memory limit |
|---|---|---|---|
| Kafka + Mongo + Couchbase | 0.7 | ~2.4 Gi | ~3.1 Gi |
| ingest, generator, risk, gateway, agent, frontend | 0.47 | ~1.1 Gi | ~2.1 Gi |
| spark-aggregates (only while the CronJob runs) | 0.5 | 1 Gi | 2 Gi |
| kind control plane (not in charts) | – | ~0.6 Gi typical | – |

So plan for roughly **4–5 GiB of Docker memory** for the steady state, plus 2 GiB while Spark runs.
With Docker Desktop at 8 GB, stop the compose stack (`make down`) before `make deploy-local`.

## Known gaps

- HPAs need metrics-server, which kind doesn't ship; without it they show `<unknown>` targets (objects
  and scaling config are still valid). Install: `kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.8.0/components.yaml`
  and add `--kubelet-insecure-tls` to its args for kind.
- risk restarts until the Couchbase init Job has created the bucket (no Couchbase-specific wait).
- The OpenShift and Harness configs are statically linted only; they need the human steps in their READMEs.
