#!/usr/bin/env bash
# Fire every alert once, on purpose, on the running cluster (spec §10.5). Nothing in any
# manifest changes: the payer's test controls and replica counts do it, and are put back.
# The invariants are not expected to hold for the batches used here.
source "$(dirname "$0")/env.sh"
require_our_cluster
cd "$ROOT"

say() { echo "[$(date +%H:%M:%S)] $*"; }
run=alerts-$(date +%s)

# The mock payer's test controls, from a worker: the only pods allowed to reach it.
payer() { # method [json]
  k exec deployment/worker -- python -c "
import httpx2, os, sys
r = httpx2.request(sys.argv[1], os.environ['WORKER_PAYER_URL'] + '/profile', content=sys.argv[2] or None,
                   headers={'Content-Type': 'application/json'}, timeout=30)
r.raise_for_status()" "$1" "${2:-}"
}
# No faults, so that nothing but the limiter sets the pace.
CALM='"throttle_rate":0,"server_error_rate":0,"malformed_rate":0,"slow_rate":0,"timeout_rate":0,"late_rate":0'

submit() { # count name
  batch=$(k exec -i deployment/api -- python - "$1" "$run-$2" <scripts/k8s/submit.py)
}

finish() { # wait until the last batch is done
  local answer="" deadline=$((SECONDS + 600))
  # A failed call is not an answer: keep asking.
  until answer=$(k exec -i deployment/api -- python - "$batch" <scripts/k8s/status.py) &&
    [ "${answer#* }" = done ]; do
    [ $SECONDS -lt $deadline ] || { echo "Batch not done: $answer" >&2; exit 1; }
    sleep 5
  done
}

state_of() { # alert name
  k exec deployment/prometheus -- wget -qO- http://127.0.0.1:9090/api/v1/alerts | python3 -c '
import json, sys
found = [a["state"] for a in json.load(sys.stdin)["data"]["alerts"] if a["labels"]["alertname"] == sys.argv[1]]
print("firing" if "firing" in found else found[0] if found else "inactive")' "$1"
}

fires() { # alert name, seconds to wait at most
  local started=$SECONDS
  until [ "$(state_of "$1")" = firing ]; do
    [ $((SECONDS - started)) -lt "$2" ] || { echo "$1 did not fire within $2 s" >&2; exit 1; }
    sleep 3
  done
  say "$1 fired after $((SECONDS - started)) s"
}

quiet() { # alert names: not already firing, or firing now would prove nothing
  local name
  for name in "$@"; do
    [ "$(state_of "$name")" != firing ] || { echo "$name is already firing" >&2; exit 1; }
  done
}

restore() {
  k scale deployment/metrics --replicas=1 >/dev/null
  k scale deployment/worker --replicas=2 >/dev/null
  k rollout status deployment/worker --timeout=300s >/dev/null
  payer DELETE
}
trap restore EXIT

restore
quiet PayerRateNearLimit PayerOverLimit ExceptionsRising LeaseStuck TelemetryMissing

say "scaling the metrics process to 0"
k scale deployment/metrics --replicas=0
fires TelemetryMissing 180
k scale deployment/metrics --replicas=1
k rollout status deployment/metrics --timeout=300s

say "a payer whose limit is 17 a second, while the workers send 16"
payer PATCH "{$CALM,\"rate_limit_per_second\":17}"
submit 3000 pace
fires PayerRateNearLimit 240

say "lowering the payer's limit to 8"
payer PATCH '{"rate_limit_per_second":8}'
fires PayerOverLimit 60
payer DELETE
finish

say "a payer that fails every request"
payer PATCH "{$CALM,\"server_error_rate\":1}"
submit 150 failing
fires ExceptionsRising 240
payer DELETE
finish

say "killing every worker in the middle of its calls"
payer PATCH "{$CALM,\"latency_min_seconds\":20,\"latency_max_seconds\":20}"
submit 100 stuck
sleep 5
# Kill first, then scale. Scaling first asks the workers to stop, and they finish their calls
# before they go. The replacements are removed before they have started.
k delete pods -l app.kubernetes.io/name=worker --grace-period=0 --force
k scale deployment/worker --replicas=0
k wait --for=delete pod -l app.kubernetes.io/name=worker --timeout=120s
# Without a lease left behind there is nothing to alert on: say so now, not in five minutes.
sleep 10
left=$(k exec deployment/prometheus -- wget -qO- \
  'http://127.0.0.1:9090/api/v1/query?query=coverage_check_checks%7Bstate%3D%22leased%22%7D' |
  python3 -c 'import json, sys; print(int(float(json.load(sys.stdin)["data"]["result"][0]["value"][1])))')
[ "$left" -gt 0 ] || { echo "The killed workers left no lease behind" >&2; exit 1; }
say "$left checks left leased with no worker"
fires LeaseStuck 300
restore
finish

say "every alert fired once"
