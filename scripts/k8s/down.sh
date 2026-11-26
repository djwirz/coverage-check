#!/usr/bin/env bash
# Delete the cluster this repo created, and nothing else.
source "$(dirname "$0")/env.sh"
fetch_tools
"$KIND" delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG"
