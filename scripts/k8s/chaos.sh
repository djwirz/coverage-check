#!/usr/bin/env bash
# Disturb a running batch and then check the invariants (spec §9). Each step waits for the
# batch to have got far enough, not for a clock, and fails if the batch finished first.
# CHAOS_COUNT sets the batch size.
source "$(dirname "$0")/env.sh"
require_our_cluster
cd "$ROOT"
count=${CHAOS_COUNT:-1500}

say() { echo "[$(date +%H:%M:%S)] $*"; }

# Sets `final` and `state`. While Postgres or an API pod is away it keeps the last answer.
final=0
state=running
look() {
  local answer
  if answer=$(k exec -i deployment/api -- python - "$batch" <scripts/k8s/status.py 2>/dev/null); then
    read -r final state <<<"$answer"
  fi
}

reach() { # percent of checks final
  local want=$((count * $1 / 100)) deadline=$((SECONDS + 600))
  while look; [ "$final" -lt "$want" ]; do
    [ $SECONDS -lt $deadline ] || { echo "Stuck at $final of $count, waiting for $want" >&2; exit 1; }
    sleep 2
  done
  if [ "$state" = done ]; then
    echo "The batch finished before the chaos did. Use a larger CHAOS_COUNT." >&2
    exit 1
  fi
  say "$final of $count final"
}

# Talk to the mock payer's test controls from a worker, the only pods allowed to.
payer() { # method [json]
  k exec deployment/worker -- python -c "
import httpx2, os, sys
r = httpx2.request(sys.argv[1], os.environ['WORKER_PAYER_URL'] + '/profile', content=sys.argv[2] or None,
                   headers={'Content-Type': 'application/json'}, timeout=30)
r.raise_for_status()" "$1" "${2:-}"
}

k rollout status deployment/worker --timeout=300s
batch=$(k exec -i deployment/api -- python - "$count" "chaos-$(date +%s)" <scripts/k8s/submit.py)
say "submitted $count checks as batch $batch"

reach 5
# Slow the payer for a moment, so that every worker task is surely in a call, then kill one
# worker outright: its leases and slots have to run out by themselves.
payer PATCH '{"latency_min_seconds": 3, "latency_max_seconds": 3}'
sleep 4
# Not mapfile: macOS still has bash 3.2. Pod names have no spaces.
workers=($(k get pods -l app.kubernetes.io/name=worker -o name))
say "killing ${workers[0]} outright, and deleting ${workers[1]}"
k delete "${workers[0]}" --grace-period=0 --force
k delete "${workers[1]}" --wait=false
k rollout status deployment/worker --timeout=300s
payer DELETE

reach 15
say "scaling workers to 10"
k scale deployment/worker --replicas=10
k rollout status deployment/worker --timeout=300s
reach 30
say "scaling workers to 1"
k scale deployment/worker --replicas=1
reach 40
say "scaling workers to 3"
k scale deployment/worker --replicas=3
k rollout status deployment/worker --timeout=300s

reach 50
before=$(k get pod postgres-0 -o 'jsonpath={.metadata.uid}')
say "deleting the Postgres pod"
k delete pod postgres-0
k rollout status statefulset/postgres --timeout=300s
[ "$(k get pod postgres-0 -o 'jsonpath={.metadata.uid}')" != "$before" ] || {
  echo "Postgres is the same pod as before" >&2
  exit 1
}
k rollout status deployment/worker --timeout=300s

reach 65
say "degrading the payer"
degraded_from=$(date +%s)
payer PATCH '{"server_error_rate": 0.3, "throttle_rate": 0.2}'
reach 75
say "restoring the payer"
payer DELETE
degraded_until=$(date +%s)

reach 80
k rollout status deployment/api --timeout=300s
apis=($(k get pods -l app.kubernetes.io/name=api -o name))
evict() { # pod/name
  printf '{"apiVersion":"policy/v1","kind":"Eviction","metadata":{"name":"%s","namespace":"%s"}}' \
    "${1#pod/}" "$NAMESPACE" |
    k create --raw "/api/v1/namespaces/$NAMESPACE/pods/${1#pod/}/eviction" -f - >/dev/null
}
say "evicting both API pods"
evict "${apis[0]}"
if evict "${apis[1]}" 2>.cache/eviction.log; then
  echo "The PodDisruptionBudget let both API pods be evicted" >&2
  exit 1
fi
grep -qi "disruption budget" .cache/eviction.log || { cat .cache/eviction.log >&2; exit 1; }
say "the second eviction was refused by the disruption budget"
k rollout status deployment/api --timeout=300s

deadline=$((SECONDS + 900))
while look; [ "$state" != done ]; do
  [ $SECONDS -lt $deadline ] || { echo "Not done: $final of $count final" >&2; exit 1; }
  sleep 5
done
say "done: $final of $count final"
k rollout status deployment/worker --timeout=300s
k exec deployment/worker -- coverage-check report "$batch"
k exec -i deployment/worker -- python - "$batch" "$degraded_from" "$degraded_until" <scripts/k8s/evidence.py
