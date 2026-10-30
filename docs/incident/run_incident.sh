#!/usr/bin/env bash
# Incident exercise (kind): a typo'd ConfigMap value takes the risk service down on its next restart.
# Usage: scripts run against the `ledgerlens` namespace; output is the raw evidence for docs/postmortem.md.
set -uo pipefail
NS=ledgerlens
ts() { date -u +%H:%M:%S; }
lag() { kubectl exec -n $NS kafka-0 -- /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
          --describe --group risk 2>/dev/null | awk 'NR>1 && $6 ~ /^[0-9]+$/ {l+=$6} END{print l+0}'; }
# shellcheck disable=SC2016  # $MONGO_INITDB_* must expand inside the mongo container, not here
flagged() { kubectl exec -n $NS mongo-0 -- sh -c 'mongosh -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" --quiet --eval "db.getSiblingDB(\"ledgerlens\").transactions.countDocuments({\"risk.flagged\":true})"' 2>/dev/null | tail -1; }
pod() { kubectl get pods -n $NS -l app.kubernetes.io/name=risk --no-headers 2>/dev/null | awk '{print $2, $3, "restarts=" $4}' | tr '\n' ';'; }
sample() { echo "$(ts) | risk pod: $(pod) | consumer lag(risk): $(lag) | flagged in mongo: $(flagged)"; }

echo "== $(ts) baseline"; sample
echo "== $(ts) INJECT: ConfigMap edit with a typo (RISK_FLAG_THRESHOLD 50 -> 5O)"
kubectl patch cm risk -n $NS --type merge -p '{"data":{"RISK_FLAG_THRESHOLD":"5O"}}'
echo "== $(ts) running pod keeps old env (ConfigMap env is read at start)"; sample
echo "== $(ts) pod rescheduled (simulated node drain: delete pod)"
kubectl delete pod -n $NS -l app.kubernetes.io/name=risk --wait=false
for _ in 1 2 3 4 5 6 7 8; do sleep 15; sample; done

echo "== $(ts) DIAGNOSE"
kubectl get pods -n $NS -l app.kubernetes.io/name=risk
P=$(kubectl get pods -n $NS -l app.kubernetes.io/name=risk -o jsonpath='{.items[0].metadata.name}')
kubectl get pod -n $NS "$P" -o jsonpath='last exit code: {.status.containerStatuses[0].lastState.terminated.exitCode}{"\n"}'
echo "-- logs --previous:"; kubectl logs -n $NS "$P" --previous 2>&1 | grep -iE "critical|invalid|error" | head -5
echo "-- configmap:"; kubectl get cm risk -n $NS -o jsonpath='{.data.RISK_FLAG_THRESHOLD}{"\n"}'

echo "== $(ts) FIX: restore value, restart"
kubectl patch cm risk -n $NS --type merge -p '{"data":{"RISK_FLAG_THRESHOLD":"50"}}'
kubectl rollout restart deploy/risk -n $NS
kubectl rollout status deploy/risk -n $NS --timeout=180s
for _ in 1 2 3 4 5 6; do sleep 15; sample; done
echo "== $(ts) done"
