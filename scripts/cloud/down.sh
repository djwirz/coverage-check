#!/usr/bin/env bash
# Destroy the demo cluster and everything in it. Safe to run twice, or after a half-made
# deploy. The namespace goes first, while the disk driver is still there to delete the
# volumes: destroying the stack alone would leave them behind, and billing.
export COVERAGE_CHECK_TARGET=demo
source "$(dirname "$0")/../k8s/env.sh"
cd "$ROOT"

fetch_tools

empty_cluster() {
  "$ROOT/scripts/cloud/connect.sh" || return 1
  require_our_cluster
  k delete namespace "$NAMESPACE" --ignore-not-found --timeout=600s || return 1
  local deadline=$((SECONDS + 300))
  while [ -n "$(k get persistentvolumes -o name)" ]; do
    [ $SECONDS -lt $deadline ] || return 1
    sleep 5
  done
}

# In a subshell, so that nothing in there can stop the stack from being destroyed.
if [ -x "$KUBECTL" ] && aws eks describe-cluster --name "$CLUSTER" >/dev/null 2>&1; then
  (empty_cluster) || echo "WARNING: could not empty the cluster; check for leftover volumes." >&2
fi
npx cdk destroy Demo --force
