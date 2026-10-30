# OpenShift

The same umbrella chart deploys to OpenShift with `values-openshift.yaml`. It differs from kind in three ways:

1. **Arbitrary UIDs (restricted-v2 SCC).** OpenShift runs each pod as a random UID from the namespace's
   range, always with GID 0, and rejects pods that ask for a specific `runAsUser`. Accordingly:
   - No LedgerLens chart sets `runAsUser`/`fsGroup`. The overlay also nulls `global.kafkaToolsRunAsUser`,
     so the wait-for-topics initContainer takes the SCC's UID.
   - The Python image (root `Dockerfile`) creates its user with `--gid 0`, and `/app/.venv` is only read
     at runtime. The process writes only to `/tmp`, which is an `emptyDir` in the charts (read-only root FS).
     So an unknown UID in group 0 works. Verify with:
     `docker run --rm -u 12345:0 --read-only --tmpfs /tmp ledgerlens/ingest:dev python -c "import ingest"`.
   - The Spark image runs as UID 185 upstream. Its entrypoint copes with a UID that has no passwd entry,
     and the chart sets `HOME=/tmp`.
2. **Routes.** `openshift.routes.enabled=true` renders `route.openshift.io/v1` Routes for gateway and
   frontend, with edge TLS, HTTP redirected to HTTPS, and a 1h timeout on the gateway for the WebSocket stream.
3. **No in-cluster infra.** The kind StatefulSets aren't portable to restricted-v2:
   - `apache/kafka` must write to `/opt/kafka/config`.
   - Couchbase CE starts as root.

   Use Strimzi from OperatorHub for Kafka (its bootstrap Service is `my-cluster-kafka-bootstrap:9092`,
   already set in the overlay), MongoDB Atlas or the MongoDB operator, and Couchbase Capella.

## HUMAN ACTION REQUIRED

1. Get a free Developer Sandbox at https://developers.redhat.com/developer-sandbox and open the web console.
2. Copy the login command: user menu → **Copy login command** → **Display token**. Then run:
   ```sh
   oc login --token=<token> --server=https://api.<sandbox-cluster>:6443
   oc project <your-username>-dev   # the sandbox pre-creates this project; you can't run `oc new-project` there
   ```
   On a full cluster, use `oc new-project ledgerlens` instead.
3. Make the images pullable. Pick one:
   - Push them from CI to GHCR (`.github/workflows/ci.yml` does this on `main`) and make the packages
     public. For private packages, run
     `oc create secret docker-registry ghcr --docker-server=ghcr.io --docker-username=<user> --docker-password=<PAT>`
     and then `oc secrets link default ghcr --for=pull`.
   - Or push to the internal registry:
     `oc registry login`, then tag and push to
     `default-route-openshift-image-registry.<apps-domain>/<project>/ledgerlens-<svc>:<tag>`.

   Then replace `ghcr.io/CHANGE_ME` in `values-openshift.yaml`.
4. Provide Kafka, Mongo and Couchbase. In the sandbox, Strimzi may not be installable (no cluster-admin).
   A managed Kafka, Atlas and Capella on free tiers are the practical route. Update the
   `KAFKA_BOOTSTRAP` and `COUCHBASE_URL` values in the overlay.
5. Create the Secret. Never commit it.
   ```sh
   oc create secret generic ledgerlens-secrets \
     --from-literal=MONGO_URI='mongodb+srv://...' \
     --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
     --from-literal=COUCHBASE_USER=... --from-literal=COUCHBASE_PASSWORD=... \
     --from-literal=GOOGLE_API_KEY=...        # or ANTHROPIC_API_KEY, for the agent
   ```
6. Install:
   ```sh
   scripts/helm_deps.sh
   helm upgrade --install ledgerlens deploy/helm/ledgerlens -f deploy/openshift/values-openshift.yaml --wait --timeout 15m
   oc get pods,routes
   ```
7. Smoke test: `NAMESPACE=<project> JWT_SECRET=<same secret> scripts/smoke_test.sh`. It port-forwards,
   so it works the same as on kind. Alternatively, `curl https://$(oc get route gateway -o jsonpath='{.spec.host}')/health/ready`.

The sandbox caps a project at roughly 7 GiB of memory. The service pods without infra request about 1.1 GiB, so they fit.
