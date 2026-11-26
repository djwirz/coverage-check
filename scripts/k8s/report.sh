#!/usr/bin/env bash
# The invariant report for a batch, run inside the cluster. Usage: report.sh <batch id>
source "$(dirname "$0")/env.sh"
require_our_cluster
k exec deployment/worker -- coverage-check report "${1:?usage: report.sh <batch id>}"
