#!/usr/bin/env bash
# End to end on the running cluster: submit, wait, check the invariants, and validate the
# stored FHIR results. E2E_COUNT sets the batch size; E2E_FHIR=0 skips the validator.
source "$(dirname "$0")/env.sh"
require_our_cluster
cd "$ROOT"
count=${E2E_COUNT:-200}

"$ROOT/scripts/k8s/netpol.sh"

batch=$(k exec -i deployment/api -- python - "$count" "e2e-$(date +%s)" 600 <scripts/k8s/submit.py)
echo "Batch $batch is done."
k exec deployment/worker -- coverage-check report "$batch"

if [ "${E2E_FHIR:-1}" = 1 ]; then
  results=.cache/e2e-results
  rm -rf "$results" && mkdir -p "$results"
  k exec -i deployment/api -- python - "$batch" <scripts/k8s/results.py |
    (cd "$results" && split -l 1 - result-)
  for file in "$results"/result-*; do mv "$file" "$file.json"; done
  stored=$(find "$results" -name '*.json' | wc -l | tr -d ' ')
  [ "$stored" = "$count" ] || { echo "Expected $count results, got $stored" >&2; exit 1; }
  scripts/validate_fhir.sh "$results"
fi
