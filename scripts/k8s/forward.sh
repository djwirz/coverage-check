#!/usr/bin/env bash
# Open one of the web pages in the cluster on this machine, until Ctrl-C.
# Usage: scripts/k8s/forward.sh dashboard|traces
source "$(dirname "$0")/env.sh"
require_our_cluster
case ${1:-} in
dashboard)
  echo "Dashboard: http://127.0.0.1:3000   Alerts: http://127.0.0.1:9090/alerts"
  k port-forward service/prometheus 9090:9090 >/dev/null &
  trap 'kill %1 2>/dev/null || true' EXIT
  k port-forward service/grafana 3000:3000
  ;;
traces)
  echo "Traces: http://127.0.0.1:16686"
  k port-forward service/jaeger 16686:16686
  ;;
*) echo "usage: $0 dashboard|traces" >&2; exit 2 ;;
esac
