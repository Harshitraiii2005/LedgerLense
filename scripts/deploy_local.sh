#!/usr/bin/env bash
# `make deploy-local`: kind cluster -> build + load images -> Secret from .env -> helm install -> smoke test.
# Needs: docker, kind, kubectl, helm, uv. Idempotent: re-running upgrades in place.
set -euo pipefail

CLUSTER="${CLUSTER:-ledgerlens}"
NS="${NAMESPACE:-ledgerlens}"
# shellcheck source=/dev/null # .env is local and gitignored
set -a && . ./.env && set +a

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config deploy/kind/cluster.yaml --name "$CLUSTER"
fi
kubectl config use-context "kind-$CLUSTER"

load() { kind load docker-image "$1" --name "$CLUSTER"; }
build() { # name, docker build args...
  docker build -t "ledgerlens/$1:dev" "${@:2}"
  load "ledgerlens/$1:dev"
}

# Charts for code that doesn't exist yet in this checkout are switched off instead of failing.
SET=()
for svc in generator ingest risk gateway agent; do
  if [ -d "services/$svc" ]; then build "$svc" --build-arg SERVICE="$svc" .; else SET+=(--set "$svc.enabled=false"); fi
done
if [ -f frontend/Dockerfile ]; then build frontend frontend; else SET+=(--set frontend.enabled=false); fi
if [ -f jobs/spark_aggregates/Dockerfile ]; then
  build spark-aggregates --target runtime jobs/spark_aggregates
else
  SET+=(--set spark-aggregates.enabled=false)
fi

# Pre-load infra images so the kind node doesn't pull them itself. These are multi-arch
# indexes; with Docker Desktop's containerd image store `kind load docker-image` fails on them
# ("content digest ... not found"), so export only the node's platform and load the archive.
PLATFORM="linux/$(docker version --format '{{.Server.Arch}}')"
ARCHIVE="$(mktemp -t ledgerlens-img).tar"
trap 'rm -f "$ARCHIVE"' EXIT
for img in apache/kafka:3.9.1 mongo:8.0.34 couchbase/server:community-8.0.0; do
  docker image inspect "$img" > /dev/null 2>&1 || docker pull -q --platform "$PLATFORM" "$img"
  docker save --platform "$PLATFORM" -o "$ARCHIVE" "$img"
  kind load image-archive "$ARCHIVE" --name "$CLUSTER"
done

# One Secret with every credential, built from .env (gitignored). Values never touch the repo.
# Note: MONGO_ROOT_PASSWORD is embedded in a URI, so keep it URL-safe (or percent-encode it).
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -
OPTIONAL=()
[ -n "${GOOGLE_API_KEY:-}" ] && OPTIONAL+=(--from-literal=GOOGLE_API_KEY="$GOOGLE_API_KEY")
[ -n "${ANTHROPIC_API_KEY:-}" ] && OPTIONAL+=(--from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY")
[ -n "${OPENAI_API_KEY:-}" ] && OPTIONAL+=(--from-literal=OPENAI_API_KEY="$OPENAI_API_KEY")
kubectl -n "$NS" create secret generic ledgerlens-secrets \
  --from-literal=MONGO_ROOT_USER="$MONGO_ROOT_USER" \
  --from-literal=MONGO_ROOT_PASSWORD="$MONGO_ROOT_PASSWORD" \
  --from-literal=MONGO_URI="mongodb://$MONGO_ROOT_USER:$MONGO_ROOT_PASSWORD@mongo:27017/?authSource=admin" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --from-literal=COUCHBASE_USER="$COUCHBASE_USER" \
  --from-literal=COUCHBASE_PASSWORD="$COUCHBASE_PASSWORD" \
  ${OPTIONAL[@]+"${OPTIONAL[@]}"} \
  --dry-run=client -o yaml | kubectl apply -f -

scripts/helm_deps.sh
helm upgrade --install ledgerlens deploy/helm/ledgerlens -n "$NS" \
  -f deploy/helm/ledgerlens/values-kind.yaml ${SET[@]+"${SET[@]}"} --wait --timeout 15m

NAMESPACE="$NS" scripts/smoke_test.sh
