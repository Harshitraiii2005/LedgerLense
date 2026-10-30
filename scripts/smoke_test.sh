#!/usr/bin/env bash
# Post-deploy smoke test. Works against any cluster the current kube context points at.
#   NAMESPACE=ledgerlens scripts/smoke_test.sh
# Exits non-zero on the first failed check.
set -euo pipefail

NS="${NAMESPACE:-ledgerlens}"
TIMEOUT="${TIMEOUT:-600s}"
PIDS=()
# shellcheck disable=SC2329 # invoked via the EXIT trap
cleanup() { for p in "${PIDS[@]:-}"; do [ -n "$p" ] && kill "$p" 2> /dev/null || true; done; }
trap cleanup EXIT

step() { printf '\n==> %s\n' "$*"; }
fail() { printf 'SMOKE TEST FAILED: %s\n' "$*" >&2; exit 1; }

step "waiting for workloads in namespace $NS (timeout $TIMEOUT)"
kubectl -n "$NS" wait --for=condition=complete job --all --timeout="$TIMEOUT"
kubectl -n "$NS" wait --for=condition=Available deployment --all --timeout="$TIMEOUT"
for sts in $(kubectl -n "$NS" get statefulset -o name); do
  kubectl -n "$NS" rollout status "$sts" --timeout="$TIMEOUT"
done
kubectl -n "$NS" get pods -o wide

# Port-forward each HTTP service to a local port (offset by 10000 to avoid clashing with compose).
forward() { # service port
  kubectl -n "$NS" port-forward "svc/$1" "$(($2 + 10000)):$2" > /dev/null 2>&1 &
  PIDS+=($!)
}
SERVICES=$(kubectl -n "$NS" get svc -l app.kubernetes.io/part-of=ledgerlens,helm.sh/chart -o jsonpath='{range .items[*]}{.metadata.name}:{.spec.ports[0].port}{"\n"}{end}')
for entry in $SERVICES; do forward "${entry%%:*}" "${entry##*:}"; done
sleep 3

step "readiness endpoints"
for entry in $SERVICES; do
  name="${entry%%:*}"; port=$((${entry##*:} + 10000))
  path=/health/ready; [ "$name" = frontend ] && path=/
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$port$path" || true)
  echo "$name $path -> $code"
  [ "$code" = 200 ] || fail "$name not ready (HTTP $code)"
done

step "API calls through the gateway"
GW="http://localhost:18002"
# Mint a token locally with the same secret the cluster uses (from .env, which `make deploy-local` loaded).
: "${JWT_SECRET:?JWT_SECRET must be set (source .env)}"
TOKEN=$(uv run --quiet python services/gateway/scripts/mint_token.py --sub smoke-test --role analyst)
AUTH="Authorization: Bearer $TOKEN"

total() { curl -sf -H "$AUTH" "$GW/v1/transactions?page_size=1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["total"])'; }
first=$(total) || fail "/v1/transactions failed"
echo "v1 total=$first"
curl -sf -H "$AUTH" "$GW/v2/transactions?limit=5" | python3 -c '
import json, sys
body = json.load(sys.stdin)
assert "data" in body and "next_cursor" in body, body
print("v2 data=%d next_cursor=%s" % (len(body["data"]), bool(body["next_cursor"])))
' || fail "/v2/transactions failed"

step "data is flowing (generator -> kafka -> ingest -> mongo)"
for _ in $(seq 1 12); do
  sleep 5
  now=$(total)
  echo "total=$now"
  [ "$now" -gt "$first" ] && { echo "SMOKE TEST PASSED"; exit 0; }
done
fail "transaction count did not grow from $first within 60s"
