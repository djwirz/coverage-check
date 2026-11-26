#!/usr/bin/env bash
# Shared by every cluster script. Cluster safety (CLAUDE.md): they use their own kubeconfig
# file, name the context on every call, and refuse to run against anything but the kind
# cluster this repo creates. ~/.kube/config is never read or written.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck source=tools.sh
source "$ROOT/scripts/k8s/tools.sh"

CLUSTER=coverage-check
CONTEXT=kind-$CLUSTER
NAMESPACE=coverage-check
export KUBECONFIG="$ROOT/kubeconfig-coverage-check"
KIND="$ROOT/.cache/bin/kind-$KIND_VERSION"
KUBECTL="$ROOT/.cache/bin/kubectl-$KUBECTL_VERSION"
IMAGE=coverage-check:local
# Stop rather than fill the disk. A new cluster has used over 6 GB (three nodes, each with
# its own copy of the images, plus swap), so ask for room before creating one.
MIN_FREE_GB=2
CREATE_FREE_GB=${CREATE_FREE_GB:-8}

k() {
  "$KUBECTL" --kubeconfig "$KUBECONFIG" --context "$CONTEXT" --namespace "$NAMESPACE" "$@"
}

cluster_exists() {
  "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"
}

require_our_cluster() {
  local current server
  cluster_exists || { echo "No kind cluster '$CLUSTER'. Run: make up" >&2; exit 1; }
  current=$("$KUBECTL" --kubeconfig "$KUBECONFIG" config current-context 2>/dev/null || true)
  server=$("$KUBECTL" --kubeconfig "$KUBECONFIG" config view --minify --context "$CONTEXT" \
    -o 'jsonpath={.clusters[0].cluster.server}' 2>/dev/null || true)
  if [ "$current" != "$CONTEXT" ] || [[ "$server" != https://127.0.0.1:* ]]; then
    echo "Refusing: context '$current' at '$server' is not this repo's kind cluster." >&2
    exit 1
  fi
}

require_free_disk() { # gigabytes
  local free
  free=$(($(df -Pk "$ROOT" | awk 'NR==2 {print $4}') / 1024 / 1024))
  if [ "$free" -lt "${1:-$MIN_FREE_GB}" ]; then
    echo "Only ${free} GB free on disk; want at least ${1:-$MIN_FREE_GB}. Stopping." >&2
    exit 1
  fi
}
