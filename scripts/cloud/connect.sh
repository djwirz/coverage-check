#!/usr/bin/env bash
# Write the demo cluster's kubeconfig, to its own file, using the admin role.
export COVERAGE_CHECK_TARGET=demo
source "$(dirname "$0")/../k8s/env.sh"
account=$(aws sts get-caller-identity --query Account --output text)
aws eks update-kubeconfig --name "$CLUSTER" --alias "$CONTEXT" --kubeconfig "$KUBECONFIG" \
  --role-arn "arn:aws:iam::$account:role/coverage-check-demo-admin" >/dev/null
