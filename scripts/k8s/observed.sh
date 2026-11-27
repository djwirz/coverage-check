#!/usr/bin/env bash
# Check what Prometheus and Jaeger in the cluster hold (observed.py).
source "$(dirname "$0")/env.sh"
require_our_cluster
cd "$ROOT"

# Port-forwards go through the API server, so NetworkPolicies don't apply to them.
k port-forward service/prometheus 19090:9090 >/dev/null &
prometheus=$!
k port-forward service/jaeger 26686:16686 >/dev/null &
jaeger=$!
trap 'kill "$prometheus" "$jaeger" 2>/dev/null || true' EXIT

python3 scripts/k8s/observed.py http://127.0.0.1:19090 http://127.0.0.1:26686
