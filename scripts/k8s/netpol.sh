#!/usr/bin/env bash
# Prove the NetworkPolicies are enforced: what should connect does, what shouldn't can't.
# A policy that the cluster's networking ignores would look exactly like a working one.
source "$(dirname "$0")/env.sh"
require_our_cluster

# The pod itself says what happened, so that an exec that fails can't pass for "blocked".
probe() { # from-deployment host port
  k exec "deployment/$1" -- python -c "
import socket
try:
    socket.create_connection(('$2', $3), timeout=5).close()
    print('yes')
except OSError:
    print('no')
"
}

failed=0
expect() { # yes|no from host port
  local got
  got=$(probe "$2" "$3" "$4") || got="could not run"
  if [ "$got" = "$1" ]; then
    echo "ok    $2 -> $3:$4 connects: $got"
  else
    echo "FAIL  $2 -> $3:$4 connects: $got, expected $1"
    failed=1
  fi
}

expect yes worker mock-payer 8000
expect yes worker postgres 5432
expect yes api postgres 5432
expect yes metrics postgres 5432
expect yes api otel-collector 4318
expect yes worker otel-collector 4318
# The same targets, from pods that aren't allowed.
expect no api mock-payer 8000
expect no mock-payer postgres 5432
expect no worker api 8000
# The payer's metrics port is for Prometheus, and reading it gives no way to the other one.
expect no worker mock-payer 9100
expect no metrics mock-payer 8000
expect no api metrics 9000
expect no api prometheus 9090
expect no worker jaeger 4317
expect no mock-payer otel-collector 4318
exit $failed
