#!/usr/bin/env bash
# Check the Prometheus and collector configs, and test the alert rules, with the same pinned
# images the cluster runs. Needs Docker, not a cluster.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
config=$ROOT/deploy/base/observability

image() { sed -n 's/^ *image: //p' "$config/$1.yaml"; }

promtool() {
  docker run --rm --volume "$ROOT:/repo:ro" --workdir /repo --entrypoint /bin/promtool \
    "$(image prometheus)" "$@"
}
promtool check config deploy/base/observability/prometheus.yml
promtool test rules tests/prometheus/alerts_test.yml

docker run --rm --volume "$config/otel-collector.yml:/etc/otelcol/config.yaml:ro" \
  "$(image otel-collector)" validate --config=/etc/otelcol/config.yaml
echo "collector config: ok"
