# 2. Kubernetes on kind, locally and in CI

Date: 2026-11-23
Status: Accepted

## Context
The claims this project makes depend on things a scheduler does:
- pods killed and rescheduled mid-batch,
- replicas scaled up and down,
- SIGTERM with a grace period,
- probes that decide when a pod gets traffic.

Running the processes by hand, or in Docker Compose, can imitate some of this. But "it survives Kubernetes" only means something on Kubernetes. A managed cluster isn't free, and the project has to run from one command on a laptop (build-plan ROADMAP, phase 5).

## Options considered
1. **kind** (Kubernetes in Docker). An upstream Kubernetes SIG project, made for testing Kubernetes itself. It runs in GitHub Actions without much effort and supports multi-node clusters from one config file.
2. **k3d** (k3s in Docker). Light and fast, with a built-in registry and load balancer. But it's k3s rather than upstream Kubernetes, with its own defaults.
3. **minikube.** Mature, with many drivers and add-ons. Heavier, and less often used in CI.
4. **A managed cluster from day one** (EKS, GKE, AKS, or a smaller provider). It's the real thing, but it costs money from the first slice and slows every iteration.

## Decision
Option 1. kind for the laptop and for CI, with one cluster config in the repo (a control plane and a couple of workers, so pod rescheduling is real). Kubernetes arrives at slice 4. Slices 1 to 3 build and test the code with Compose first.

## Why
The same cluster type on the laptop and in CI means the end-to-end and chaos tests in CI are the ones run by hand. It costs nothing. Upstream Kubernetes keeps what's learned portable to any managed cluster later. Starting on Kubernetes only after the code is correct means a failure in slices 4 and 5 is about Kubernetes, not about the queue logic.

## Consequences
- No public URL from kind. Whether there's a public deploy is a separate decision (spec §13 Q3).
- kind's default networking may not enforce NetworkPolicy. That's checked at slice 4 (spec §13 Q1).
- Images are built locally and loaded into the cluster, with no registry needed until there's a cloud deploy.
- Exact kind, kubectl and Kubernetes versions are pinned in the repo and checked against current docs at slice 4, not taken from memory.
- Revisit with a new ADR if a cloud cluster is chosen at slice 6.
