#!/usr/bin/env bash
# What to look at when something is wrong. Secret values are not printed.
source "$(dirname "$0")/env.sh"
require_our_cluster
set +e
df -h "$ROOT"
k get pods,jobs,statefulsets,deployments,pvc -o wide
k get events --sort-by=.lastTimestamp | tail -40
k describe pods | grep -vE "^\s*$" | tail -150
for workload in statefulset/postgres job/migrate deployment/mock-payer deployment/api deployment/worker; do
  echo "--- $workload"
  k logs "$workload" --all-containers --tail=40
done
