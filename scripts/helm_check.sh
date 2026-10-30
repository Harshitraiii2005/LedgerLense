#!/usr/bin/env bash
# Static checks for every chart (used by CI and `make helm-lint`); needs helm + kubeconform, no cluster.
set -euo pipefail
cd "$(dirname "$0")/.."
K8S_VERSION="${K8S_VERSION:-1.33.0}"
# OpenShift's Route isn't a core type; validate it against the OpenShift schemas in the community catalog.
OPENSHIFT_SCHEMAS='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/openshift/v4.11-strict/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

scripts/helm_deps.sh
for chart in deploy/helm/*/; do
  helm lint --strict --quiet "$chart"
done
helm lint --strict --quiet deploy/helm/ledgerlens -f deploy/helm/ledgerlens/values-kind.yaml
helm lint --strict --quiet deploy/helm/ledgerlens -f deploy/openshift/values-openshift.yaml
echo "helm lint: ok"

for overlay in deploy/helm/ledgerlens/values-kind.yaml deploy/openshift/values-openshift.yaml; do
  echo "kubeconform: umbrella + $overlay"
  helm template ledgerlens deploy/helm/ledgerlens -n ledgerlens -f "$overlay" |
    kubeconform -strict -summary -kubernetes-version "$K8S_VERSION" \
      -schema-location default -schema-location "$OPENSHIFT_SCHEMAS"
done
