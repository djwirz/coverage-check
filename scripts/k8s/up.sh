#!/usr/bin/env bash
# Create the cluster if needed, build and load the image, apply an overlay, wait, seed.
# Usage: scripts/k8s/up.sh [local|ci]
source "$(dirname "$0")/env.sh"
OVERLAY=${1:-local}
[ -d "$ROOT/deploy/overlays/$OVERLAY" ] || { echo "No overlay '$OVERLAY'" >&2; exit 1; }
cd "$ROOT"
docker info >/dev/null 2>&1 || { echo "Docker isn't running." >&2; exit 1; }
fetch_tools

# Secrets come from .env (git-ignored), made with random values if there is none.
if [ ! -f .env ]; then
  (
    umask 077
    password=$(openssl rand -hex 24)
    sed -e "s/change-me@/$password@/" -e "s/^POSTGRES_PASSWORD=change-me/POSTGRES_PASSWORD=$password/" \
      -e "s/^COVERAGE_CHECK_API_TOKEN=change-me/COVERAGE_CHECK_API_TOKEN=$(openssl rand -hex 24)/" \
      .env.example >.env
  )
  echo "Wrote .env with new random secrets." >&2
fi
setting() { sed -n "s/^$1=//p" .env | tail -1; }
password=$(setting POSTGRES_PASSWORD)
token=$(setting COVERAGE_CHECK_API_TOKEN)
for value in "$password" "$token"; do
  if [ -z "$value" ] || [ "$value" = change-me ]; then
    echo "Set POSTGRES_PASSWORD and COVERAGE_CHECK_API_TOKEN in .env first." >&2
    exit 1
  fi
done
# It goes into a URL.
[[ $password =~ ^[A-Za-z0-9._~-]+$ ]] || { echo "Use only letters, digits and ._~- in POSTGRES_PASSWORD." >&2; exit 1; }

existed=true
if ! cluster_exists; then
  existed=false
  require_free_disk "$CREATE_FREE_GB"
  "$KIND" create cluster --name "$CLUSTER" --config deploy/kind/cluster.yaml --kubeconfig "$KUBECONFIG"
else
  "$KIND" export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG"
fi
require_our_cluster
require_free_disk

docker build -t "$IMAGE" .
"$KIND" load docker-image "$IMAGE" --name "$CLUSTER"
require_free_disk

k apply -f deploy/base/namespace.yaml
# Never in a manifest, and not on a command line where other processes could read it.
(
  umask 077
  secrets=$(mktemp)
  trap 'rm -f "$secrets"' EXIT
  {
    echo "POSTGRES_PASSWORD=$password"
    echo "COVERAGE_CHECK_API_TOKEN=$token"
    echo "DATABASE_URL=postgresql://coverage_check:$password@postgres:5432/coverage_check"
  } >"$secrets"
  k create secret generic coverage-check --from-env-file="$secrets" --dry-run=client -o yaml | k apply -f -
)

k delete job migrate --ignore-not-found
k apply -k "deploy/overlays/$OVERLAY"
if $existed; then
  # The image keeps its tag, so ask for new pods to pick it up.
  k rollout restart deployment/api deployment/worker deployment/mock-payer
fi

k rollout status statefulset/postgres --timeout=300s
k wait --for=condition=complete job/migrate --timeout=300s
for deployment in mock-payer api worker; do
  k rollout status "deployment/$deployment" --timeout=300s
done

batch=$(k exec -i deployment/api -- python - 35 make-up-sample <scripts/k8s/submit.py)
echo "Up. Sample batch $batch submitted."
echo "Check it with: make report BATCH=$batch"
