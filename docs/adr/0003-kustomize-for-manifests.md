# 3. Kustomize for the manifests, with overlays per environment

Date: 2026-11-23
Status: Accepted

## Context
The system needs the same manifests in at least two places, the laptop and CI, and maybe a cloud cluster later. The differences are small: replica counts, resource limits, image names, the failure profile, whether observability is installed. The manifests should be easy to read in review, because learning Kubernetes is part of the point.

## Options considered
1. **Plain YAML, copied per environment.** The simplest to read, but copies drift apart.
2. **Kustomize.** A base of plain manifests plus small overlays that patch them. It's built into `kubectl`, so there's nothing extra to install, and the output is plain YAML.
3. **Helm.** Templated charts with values files. It's the standard way to distribute software for others to install, but templates hide the YAML behind Go templating, and nobody else installs this.

## Decision
Option 2. `deploy/base/` holds plain manifests for each component. `deploy/overlays/local`, `deploy/overlays/ci`, and `deploy/overlays/cloud` if there is one, patch them. Third-party pieces (Prometheus and Grafana, KEDA if chosen) are installed from their own charts or manifests, pinned by version.

## Why
The base stays readable as plain Kubernetes objects, which suits a learning project and makes reviews easy. Overlays keep environments from drifting without a template language. Helm's strengths are for distributing a package to others, and that isn't this project's problem.

## Consequences
- Each overlay can be rendered with `kubectl kustomize` and checked in CI (render, plus schema validation).
- Using Helm for third-party charts while Kustomize handles our own manifests is normal, and a little untidy.
- A Helm chart of our own stays a stretch goal. If it's added, it's generated from the same base, not kept alongside it by hand.
