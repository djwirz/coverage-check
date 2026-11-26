#!/usr/bin/env bash
# The measurement the window is for: three chaos runs and one undisturbed, with the peak
# requests the payer saw in any second. Usage: measure.sh e2e|chaos
export COVERAGE_CHECK_TARGET=demo
source "$(dirname "$0")/../k8s/env.sh"
cd "$ROOT"
fetch_tools
"$ROOT/scripts/cloud/connect.sh"
mkdir -p .cache
log=.cache/demo-${1:?usage: measure.sh e2e|chaos}-$(date +%s).log
case $1 in
e2e) E2E_COUNT=3000 E2E_FHIR=0 ./scripts/k8s/e2e.sh 2>&1 | tee "$log" ;;
chaos) CHAOS_COUNT=3000 ./scripts/k8s/chaos.sh 2>&1 | tee "$log" ;;
*) echo "usage: $0 e2e|chaos" >&2; exit 2 ;;
esac
