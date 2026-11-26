#!/usr/bin/env bash
# Put the system on the demo cluster, which `cdk deploy Demo` has just created: push the
# image, apply the cloud overlay, wait. Run by .github/workflows/demo-window.yml.
# Needs AWS credentials that may assume the demo's admin role.
export COVERAGE_CHECK_TARGET=demo
source "$(dirname "$0")/../k8s/env.sh"
cd "$ROOT"
fetch_tools

"$ROOT/scripts/cloud/connect.sh"
require_our_cluster

account=$(aws sts get-caller-identity --query Account --output text)
registry=$account.dkr.ecr.$AWS_REGION.amazonaws.com
image=$registry/coverage-check:$(git rev-parse --short=12 HEAD)
admin=$(aws sts assume-role --role-arn "arn:aws:iam::$account:role/coverage-check-demo-admin" \
  --role-session-name push --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
read -r key secret session <<<"$admin"
AWS_ACCESS_KEY_ID=$key AWS_SECRET_ACCESS_KEY=$secret AWS_SESSION_TOKEN=$session \
  aws ecr get-login-password | docker login --username AWS --password-stdin "$registry" >/dev/null
docker build -t "$image" .
docker push -q "$image" >/dev/null

# New secrets every window, never in a manifest or on a command line.
k create namespace "$NAMESPACE" --dry-run=client -o yaml | k apply -f -
(
  umask 077
  secrets=$(mktemp)
  trap 'rm -f "$secrets"' EXIT
  password=$(openssl rand -hex 24)
  {
    echo "POSTGRES_PASSWORD=$password"
    echo "COVERAGE_CHECK_API_TOKEN=$(openssl rand -hex 24)"
    echo "DATABASE_URL=postgresql://coverage_check:$password@postgres:5432/coverage_check"
  } >"$secrets"
  k create secret generic coverage-check --from-env-file="$secrets" --dry-run=client -o yaml | k apply -f -
)

"$KUBECTL" kustomize deploy/overlays/cloud | sed "s|image: coverage-check:local|image: $image|" | k apply -f -
k rollout status statefulset/postgres --timeout=600s
k wait --for=condition=complete job/migrate --timeout=300s
for deployment in mock-payer api worker metrics otel-collector jaeger prometheus grafana; do
  k rollout status "deployment/$deployment" --timeout=600s
done
echo "The system is up on the demo cluster."
