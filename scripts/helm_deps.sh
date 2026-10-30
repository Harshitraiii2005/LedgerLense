#!/usr/bin/env bash
# Vendor chart dependencies. Order matters: Helm packages a file:// subchart as-is, so every
# service chart needs ledgerlens-lib in its charts/ dir before the umbrella packages it.
set -euo pipefail
cd "$(dirname "$0")/../deploy/helm"
for chart in */; do
  chart="${chart%/}"
  case "$chart" in ledgerlens | ledgerlens-lib | infra) continue ;; esac
  helm dependency build "$chart" > /dev/null
done
helm dependency build ledgerlens > /dev/null
echo "helm dependencies built"
