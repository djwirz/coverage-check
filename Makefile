# The local Kubernetes cluster (kind). Scripts act only on the cluster they create.
.PHONY: up down e2e chaos report debug

OVERLAY ?= local

up:
	./scripts/k8s/up.sh $(OVERLAY)

down:
	./scripts/k8s/down.sh

e2e:
	./scripts/k8s/e2e.sh

# CHAOS_COUNT sets the batch size.
chaos:
	./scripts/k8s/chaos.sh

report:
	./scripts/k8s/report.sh $(BATCH)

debug:
	./scripts/k8s/debug.sh
