#!/usr/bin/env bash
# Render an overlay without any cluster, to see that it builds.
source "$(dirname "$0")/env.sh"
fetch_tools
"$KUBECTL" kustomize "$ROOT/deploy/overlays/${1:?usage: render.sh <overlay>}" >/dev/null
